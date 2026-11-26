"""Website export: the ONLY bridge between the newsroom database and the website theme.

Nothing leaves the database unless it has been approved:
  * a story is exported only if editorial.can_publish(story, 'website') is true, i.e. every latest content version
    carries a valid approval bound to its content hash, verification is complete and the second check passed.
    Editing a draft voids its approval, so an edited story drops out of the next export until it is approved again;
  * live-coverage updates are exported only when individually approved and unchanged since approval;
  * corrections are exported only when published, and only for stories that are approved or published;
  * the evidence library lists only documents that belong to exported stories;
  * the breaking banner appears only for an approved story whose breaking flag has not expired.
Unapproved drafts, leads and trends are never read into the output. The file is plain JSON (assets/pm-newsroom.json)."""
from __future__ import annotations

import json
import os
import re

import hashlib
from urllib.parse import urlparse

from . import editorial as ed, linkcheck
from .util import iso, parse_iso

SCHEMA = 1
SHOW_CLAIMS = ("VERIFIED", "INDEPENDENTLY CORROBORATED", "COMPANY-REPORTED", "REPORTED ALLEGATION", "DISPUTED")
PUBLIC_CLAIM_TYPES = ("core", "figure", "quote")      # housekeeping checks ("a source exists", "names are correct") are not facts for readers
RANK = {"VERIFIED": 0, "INDEPENDENTLY CORROBORATED": 1, "COMPANY-REPORTED": 2, "REPORTED ALLEGATION": 3, "DISPUTED": 4, "UNVERIFIED": 5}


def source_id(url: str) -> str:
    return "SRC-" + hashlib.sha1((url or "").encode()).hexdigest()[:10]


def source_records(conn, sid: int) -> list[dict]:
    """The ONLY origin of links and evidence items on the website: one record per cited URL, built from stored source records
    (original URL, publisher, date published, date retrieved, archived copy). Nothing is typed by hand."""
    lr = linkcheck.latest_run(conn, sid)
    verified_at = lr["at"] if lr and lr["ok"] else ""
    out, seen = [], {}
    for c in linkcheck.claims_with_urls(conn, sid):
        url = (c["source_url"] or "").strip()
        if not url:
            continue
        if url not in seen:
            rec = linkcheck.record_for(conn, sid, url) or {}
            it = conn.execute("SELECT title, org, published_at FROM items WHERE story_id=? AND url=? LIMIT 1", (sid, url)).fetchone()
            title = _clean((it["title"] if it else "") or "") or (urlparse(url).netloc or url)
            seen[url] = {"id": source_id(url), "title": title, "publisher": rec.get("issuer") or (it["org"] if it else "") or "",
                         "published_at": rec.get("published_at") or (it["published_at"] if it else "") or "", "retrieved_at": rec.get("retrieved_at") or "",
                         "url": url, "archive_url": rec.get("wayback_url") or "", "archived_copy": bool(rec.get("wayback_url") or rec.get("full_copy_path")),
                         "classification": c["status"] or "UNVERIFIED", "supports": [], "link_verified_at": verified_at, "story_id": ed.uid(sid)}
            out.append(seen[url])
        r = seen[url]
        if RANK.get(c["status"], 5) < RANK.get(r["classification"], 5):
            r["classification"] = c["status"]
        if c["claim_type"] in PUBLIC_CLAIM_TYPES and c["claim_text"] not in r["supports"]:
            r["supports"].append(c["claim_text"])
    for r in out:
        r["supports"] = _clean("; ".join(r["supports"]))[:300] or "Primary source for this story."
    return out


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t or "")).strip()


def _summary(content: str, n: int = 300) -> str:
    for para in re.split(r"\n\s*\n", content or ""):
        p = _clean(re.sub(r"^[#>*\-\s]+", "", para))
        if len(p) > 40:
            return p if len(p) <= n else p[: n - 1].rsplit(" ", 1)[0] + "…"
    return ""


def approved_story_ids(conn) -> list[int]:
    out = []
    for r in conn.execute("SELECT id FROM stories WHERE lead_only=0 AND editorial_status IN ('approved','published') ORDER BY id"):
        ok, _ = ed.can_publish(conn, r["id"], "website")
        if ok:
            out.append(r["id"])
    return out


def _story_block(conn, sid: int, now) -> dict:
    s = conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
    vers = {v["kind"]: v for v in ed.latest_versions(conn, sid)}
    body = (vers.get("article") or vers.get("breaking_article") or {}).get("content", "")
    url_row = conn.execute("SELECT ref FROM story_links WHERE story_id=? AND kind='article_url' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    updated = max([v["created_at"] for v in vers.values()] + [s["detected_at"]])
    sources = source_records(conn, sid)
    by_url = {r["url"]: r["id"] for r in sources}
    facts = [{"text": c["claim_text"], "status": c["status"], "source_id": by_url.get((c["source_url"] or "").strip(), "")}
             for c in conn.execute("SELECT * FROM claims WHERE story_id=? ORDER BY id", (sid,))
             if c["status"] in SHOW_CLAIMS and c["claim_type"] in PUBLIC_CLAIM_TYPES]
    timeline = [{"at": i["published_at"], "text": f"{i['org']}: {_clean(i['title'])}", "url": i["url"] if i["url"] in by_url else "",
                 "source_id": by_url.get(i["url"], "")}
                for i in conn.execute("SELECT * FROM items WHERE story_id=? AND bot!='trends' AND published_at IS NOT NULL ORDER BY published_at", (sid,))]
    corr = [{"at": c["published_at"] or c["created_at"], "severity": c["severity"] or "clarification", "text": c["text"]}
            for c in conn.execute("SELECT * FROM corrections WHERE story_id=? AND status='published' ORDER BY COALESCE(published_at, created_at) DESC", (sid,))]
    return {"id": ed.uid(sid), "headline": s["headline"], "category": s["category"] or "", "summary": _summary(body),
            "body": body, "published_at": s["published_at"] or s["detected_at"], "updated_at": updated,
            "url": url_row["ref"] if url_row else "", "key_facts": facts, "timeline": timeline, "sources": sources, "corrections": corr, "links_verified_at": (sources[0]["link_verified_at"] if sources else "")}


def build(conn, now) -> dict:
    ids = approved_story_ids(conn)
    stories = [_story_block(conn, sid, now) for sid in ids]
    by_uid = {s["id"]: s for s in stories}

    breaking = None
    for r in conn.execute("SELECT id, breaking_until FROM stories WHERE breaking=1 AND breaking_until IS NOT NULL ORDER BY breaking_until DESC"):
        if r["id"] in ids and parse_iso(r["breaking_until"]) > now:
            b = by_uid[ed.uid(r["id"])]
            breaking = {"story_id": b["id"], "headline": b["headline"], "summary": b["summary"], "url": b["url"],
                        "until": r["breaking_until"], "updated_at": b["updated_at"]}
            break

    live = []
    for cov in conn.execute("SELECT * FROM live_coverage ORDER BY opened_at DESC"):
        ups = ed.live_publishable(conn, cov["story_id"])
        if not ups:
            continue    # nothing approved yet: the coverage page does not exist publicly
        live.append({"story_id": ed.uid(cov["story_id"]), "title": cov["title"], "state": cov["state"], "opened_at": cov["opened_at"],
                     "updates": [{"id": u["id"], "at": u["approved_at"] or u["created_at"], "text": u["text"],
                                  "classification": u["classification"], "source_url": u["source_url"]} for u in ups]})

    pub_ok = {r["id"] for r in conn.execute("SELECT id FROM stories WHERE editorial_status IN ('approved','published')")}
    corrections = []
    for c in conn.execute("SELECT c.*, s.headline FROM corrections c JOIN stories s ON s.id=c.story_id WHERE c.status='published' "
                          "ORDER BY COALESCE(c.published_at, c.created_at) DESC"):
        if c["story_id"] in pub_ok:
            corrections.append({"at": c["published_at"] or c["created_at"], "story_id": ed.uid(c["story_id"]), "story": c["headline"],
                                "severity": c["severity"] or "clarification", "text": c["text"]})

    evidence = []
    for s in stories:
        for src in s["sources"]:
            evidence.append(dict(src, story_id=s["id"], story=s["headline"], story_url=s["url"]))

    return {"schema": SCHEMA, "approved_only": True, "generated_at": iso(now), "stories": stories, "breaking": breaking,
            "live": live, "corrections": corrections, "evidence": evidence}


def write(conn, now, out_path: str) -> dict:
    data = build(conn, now)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    return {"stories": len(data["stories"]), "breaking": bool(data["breaking"]), "live": len(data["live"]),
            "evidence": len(data["evidence"]), "corrections": len(data["corrections"]), "out": out_path}
