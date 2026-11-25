"""Evidence archive (PRIVATE data repo only).

Per source: URL, issuer, publication time, retrieval time, HTTP status, SHA-256, a SHORT relevant extract (<=300 chars,
never a copied article) and, for key sources, an archive.org capture link (free).
Official public documents (government, courts, regulators) are saved IN FULL because they are public records;
third-party outlet pages are never copied: extract + hash + archive link only.
Nothing here is written to the public repo, and the dashboard receives rows only inside its encrypted payload."""
from __future__ import annotations

import os
import re
import requests

from . import changelog, secondcheck
from .fetch import FetchError, http_get, user_agent
from .score import is_primary_domain
from .util import iso, sha256_bytes, strip_html

MAX_EXTRACT = 300
WAYBACK_SAVE_TIMEOUT = 75


def _domain(url: str) -> str:
    return (re.sub(r"^https?://", "", url or "").split("/")[0]).lower()


def short_extract(text: str, headline: str) -> str:
    """The sentence of the page that best matches the headline's key terms, trimmed to MAX_EXTRACT characters."""
    terms = {w for w in re.findall(r"[a-z][a-z'-]{3,}", headline.lower()) if w not in secondcheck.STOP}
    best, score = "", 0
    for sent in re.split(r"(?<=[.!?])\s+", text[:60000]):
        if 40 <= len(sent) <= 600 and headline.lower() not in sent.lower() and sent.count("|") < 2:
            sc = sum(1 for w in terms if w in sent.lower())
            if sc > score:
                best, score = sent, sc
    best = best.strip() or text[:MAX_EXTRACT]
    return best if len(best) <= MAX_EXTRACT else best[: MAX_EXTRACT - 1].rsplit(" ", 1)[0] + "…"


def wayback(url: str, get=requests.get, allow_save: bool = True) -> tuple[str, str]:
    """(capture_url, status). Looks for an existing capture first; asks archive.org to save one only if none exists."""
    h = {"User-Agent": user_agent()}
    try:
        r = get("https://archive.org/wayback/available", params={"url": url}, headers=h, timeout=25)
        snap = (r.json().get("archived_snapshots") or {}).get("closest") or {}
        if snap.get("available") and snap.get("url"):
            return snap["url"].replace("http://", "https://", 1), "existing capture"
    except Exception as ex:  # noqa: BLE001
        return "", f"lookup failed: {type(ex).__name__}"
    if not allow_save:
        return "", "no existing capture"
    try:
        r = get("https://web.archive.org/save/" + url, headers=h, timeout=WAYBACK_SAVE_TIMEOUT, allow_redirects=True)
        loc = r.headers.get("Content-Location") or ""
        if r.status_code == 200 and loc.startswith("/web/"):
            return "https://web.archive.org" + loc, "captured now"
        if r.status_code == 200 and "/web/" in r.url:
            return r.url, "captured now"
        return "", f"save not confirmed (HTTP {r.status_code})"
    except Exception as ex:  # noqa: BLE001
        return "", f"save failed: {type(ex).__name__}"


def archive_story(conn, sid: int, now, data_dir: str, cfg, get=http_get, wb=wayback, max_docs: int = 4, max_wayback: int = 2) -> list[dict]:
    s = dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
    items = {r["url"]: dict(r) for r in conn.execute("SELECT url, org, published_at FROM items WHERE story_id=? ORDER BY id", (sid,))}
    urls = []
    for u in [s["primary_url"], s["original_url"]] + list(items):
        if u and u not in urls:
            urls.append(u)
    out, wb_used = [], 0
    for u in urls[:max_docs]:
        official = is_primary_domain(u, cfg)
        row = {"story_id": sid, "url": u, "issuer": (items.get(u) or {}).get("org") or s["source_org"],
               "published_at": (items.get(u) or {}).get("published_at") or s["published_at"], "retrieved_at": iso(now),
               "http_status": None, "sha256": None, "bytes": None, "extract": None, "wayback_url": None, "wayback_status": None,
               "full_copy_path": None, "official": int(bool(official)), "error": None}
        try:
            r = get(u, timeout=25, retries=2, headers={"Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.5"})
            row.update(http_status=r.status_code, sha256=sha256_bytes(r.content), bytes=len(r.content))
            text = strip_html(secondcheck.clean_html(secondcheck.decode_text(r)), limit=400_000)
            row["extract"] = short_extract(text, s["headline"])
            if official:   # public record: keep a full copy
                d = os.path.join(data_dir, "evidence", f"story-{sid}")
                os.makedirs(d, exist_ok=True)
                ext = ".pdf" if "pdf" in (r.headers.get("Content-Type", "") or "").lower() else ".html"
                rel = f"evidence/story-{sid}/{row['sha256'][:16]}{ext}"
                with open(os.path.join(data_dir, rel), "wb") as f:
                    f.write(r.content)
                row["full_copy_path"] = rel
        except Exception as ex:  # noqa: BLE001 - failures are recorded, never hidden
            row.update(http_status=getattr(ex, "status", None), error=f"{type(ex).__name__}: {ex}"[:200])
        key_source = u == s["primary_url"] or official or u == s["original_url"]
        if key_source and row["http_status"] == 200 and wb_used < max_wayback:
            row["wayback_url"], row["wayback_status"] = wb(u)
            wb_used += 1
            row["wayback_url"] = row["wayback_url"] or None
        try:
            conn.execute("INSERT OR IGNORE INTO evidence_archive(story_id,url,issuer,published_at,retrieved_at,http_status,sha256,bytes,extract,"
                         "wayback_url,wayback_status,full_copy_path,official,error) VALUES(:story_id,:url,:issuer,:published_at,:retrieved_at,:http_status,"
                         ":sha256,:bytes,:extract,:wayback_url,:wayback_status,:full_copy_path,:official,:error)", row)
        except Exception:  # noqa: BLE001
            pass
        out.append(row)
    conn.commit()
    return out


def permanent_bundle(conn, sid: int, now, data_dir: str, cfg) -> str:
    """For a published story: archive the sources (if not yet done), then write a PERMANENT bundle tied to the story ID and
    the website article URL. Files under evidence/permanent/ are never pruned and are included in every backup."""
    import json as _json
    from . import editorial as ed
    if not conn.execute("SELECT 1 FROM evidence_archive WHERE story_id=?", (sid,)).fetchone():
        archive_story(conn, sid, now, data_dir, cfg)
    rel = bundle(conn, sid, now, data_dir)
    url = conn.execute("SELECT ref FROM story_links WHERE story_id=? AND kind='article_url'", (sid,)).fetchone()
    d = os.path.join(data_dir, "evidence", "permanent", ed.uid(sid))
    os.makedirs(d, exist_ok=True)
    body = open(os.path.join(data_dir, rel), encoding="utf-8").read()
    stamp = iso(now)[:19].replace(":", "")
    name = f"bundle-{stamp}.md"
    head = f"> Website article: {url['ref'] if url else 'NOT YET LINKED (use /articleurl ID URL)'}\n> Story ID: {ed.uid(sid)}\n\n"
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(head + body)
    idx = os.path.join(d, "index.json")
    entries = _json.load(open(idx)) if os.path.exists(idx) else []
    entries.append({"story_uid": ed.uid(sid), "story_id": sid, "article_url": url["ref"] if url else None, "bundle": name,
                    "created": iso(now), "sha256": sha256_bytes((head + body).encode("utf-8"))})
    _json.dump(entries, open(idx, "w"), indent=1)
    return f"evidence/permanent/{ed.uid(sid)}/{name}"


def bundle(conn, sid: int, now, data_dir: str) -> str:
    """Write evidence/story-N/bundle.md (private repo) and return its relative path."""
    from . import editorial as ed
    s = dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
    changelog.seal(conn)
    ok, n, bad = changelog.verify(conn)
    L = [f"# Evidence bundle: {ed.uid(sid)} (story #{sid})", f"**{s['headline']}**", "",
         f"Generated {iso(now)}. Editorial status: {s['editorial_status']}. Verification: {s['verification_status']}. Category: {s['category']}. Score {s['score']}.",
         f"Change-log chain: {'INTACT' if ok else 'BROKEN at row ' + str(bad)} ({n} sealed rows; head {changelog.head(conn)[:16] or 'none'}).",
         "", "PRIVATE. Third-party extracts are short quotations for verification, not copies of articles.", "", "## Sources"]
    srcs = [dict(r) for r in conn.execute("SELECT * FROM evidence_archive WHERE story_id=? ORDER BY id", (sid,))]
    if not srcs:
        L.append("_No evidence archived yet. Run /evidence again._")
    for e in srcs:
        L += [f"### {e['issuer'] or 'unknown issuer'}{' (official public record)' if e['official'] else ''}",
              f"- URL: {e['url']}", f"- Published: {e['published_at'] or 'unknown'}", f"- Retrieved: {e['retrieved_at']} · HTTP {e['http_status']}",
              f"- SHA-256: `{e['sha256']}` ({e['bytes']} bytes)" if e["sha256"] else f"- Not retrieved: {e['error']}",
              f"- Archive.org: {e['wayback_url'] or 'none (' + (e['wayback_status'] or 'not attempted') + ')'}",
              f"- Full copy: `{e['full_copy_path']}`" if e["full_copy_path"] else "- Full copy: not kept (third-party page; extract and hash only)",
              f"- Extract: \"{e['extract']}\"" if e["extract"] else "", ""]
    L += ["## Claims", "| # | Status | Claim | Evidence / note | Source |", "|---|---|---|---|---|"]
    for c in conn.execute("SELECT * FROM claims WHERE story_id=? ORDER BY id", (sid,)):
        L.append(f"| {c['id']} | {c['status']} | {c['claim_text'].replace('|', '/')} | {((c['evidence'] or '') + ' ' + (c['note'] or '')).strip().replace('|', '/')[:200]} | {c['source_url'] or ''} |")
    sm = secondcheck.summary(conn, sid)
    L += ["", "## Second check", secondcheck.part_e_block(conn, sid), "", "## Denials and responses"]
    den = [i for i in sm["items"] if i["key"] == "denials" and i["final"] != "N/A"]
    L += [f"- [{i['final']}] {i['evidence']}" for i in den] or ["- Not assessed (run /secondcheck)."]
    L += ["", "## Approvals and versions"]
    for a in conn.execute("SELECT * FROM approvals WHERE story_id=? ORDER BY id", (sid,)):
        L.append(f"- {a['at']} · {a['action']} · {a['kind'] or 'story'}{' v' + str(a['version']) if a['version'] else ''}"
                 f"{' · dest ' + a['destination'] if a['action'] == 'approve' else ''}{'' if a['valid'] or a['action'] != 'approve' else ' · VOIDED: ' + (a['invalidated_reason'] or '')}"
                 f"{' · ' + a['reason'] if a['reason'] else ''}")
    for v in ed.latest_versions(conn, sid):
        L.append(f"- latest {v['kind']} v{v['version']} saved {v['created_at']} by {v['author']} (hash {v['content_hash'][:12]})")
    L += ["", "## Corrections"]
    L += [f"- {c['created_at']} · {c['severity']} · {c['status']} · {c['text']}" for c in conn.execute("SELECT * FROM corrections WHERE story_id=? ORDER BY id", (sid,))] or ["- none"]
    L += ["", "## Full change log (append-only)"]
    L += [f"- {r['at']} · {r['kind']} · {r['ref']} · {r['detail']} · `{(r['row_hash'] or 'unsealed')[:10]}`" for r in changelog.for_story(conn, sid)] or ["- none"]
    rel = f"evidence/story-{sid}/bundle.md"
    os.makedirs(os.path.join(data_dir, os.path.dirname(rel)), exist_ok=True)
    with open(os.path.join(data_dir, rel), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return rel
