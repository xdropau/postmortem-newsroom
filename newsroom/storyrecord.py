"""Record a story's source links, facts and notes from a fact sheet (JSON kept in the private data repo), then run the REAL checks.

Honesty rules:
- every fact starts UNVERIFIED; it only becomes a verified-type status when the real link check opened its source and found the cited text;
- the human steps (second check, Approve) are never touched here;
- NOT CONFIRMED / NEVER USE items go to story_notes, never to claims;
- a source that cannot be opened is recorded as failed, not papered over."""
from __future__ import annotations

import os

from . import changelog, evidence, linkcheck
from .fetch import http_get
from .util import iso, sha256_bytes, strip_html

STATUS_BY_KIND = {"official": "COMPANY-REPORTED", "register": "VERIFIED", "press": "REPORTED ALLEGATION"}


def _ensure_tables(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS story_notes(id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, kind TEXT NOT NULL, "
                 "text TEXT NOT NULL, created_at TEXT, UNIQUE(story_id, kind, text))")


def _note(conn, sid, kind, text, now):
    conn.execute("INSERT OR IGNORE INTO story_notes(story_id,kind,text,created_at) VALUES(?,?,?,?)", (sid, kind, text.strip(), iso(now)))


def _archived_source(conn, sid, src, now, data_dir, get):
    """A source whose live page is gone but whose archive copy we can open: store the copy itself as the record."""
    r = get(src["url"], timeout=40, retries=2)
    rel = f"evidence/story-{sid}/{sha256_bytes(r.content)[:16]}.html"
    os.makedirs(os.path.join(data_dir, os.path.dirname(rel)), exist_ok=True)
    with open(os.path.join(data_dir, rel), "wb") as f:
        f.write(r.content)
    conn.execute("INSERT OR IGNORE INTO evidence_archive(story_id,url,issuer,published_at,retrieved_at,http_status,sha256,bytes,extract,wayback_url,wayback_status,"
                 "full_copy_path,official,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                 (sid, src["url"], src["publisher"], src.get("published") or None, iso(now), r.status_code, sha256_bytes(r.content), len(r.content),
                  strip_html(r.text, limit=300), src["url"], "this IS the archive copy", rel, 1))


def record(conn, spec: dict, now, data_dir: str, cfg, get=http_get, wb=None) -> dict:
    from . import editorial as ed
    sid = int(spec["story_id"])
    if ed.is_test_only(conn, sid):
        raise ValueError("test item")
    _ensure_tables(conn)
    srcs = {s["key"]: s for s in spec["sources"]}
    log = {"story_id": sid, "sources": {}, "facts_added": 0, "notes_added": 0}
    # 1. source links (story_links) + source records
    for s in spec["sources"]:
        ed.link_story(conn, sid, "source_url", s["url"], now)
        if s.get("republished_copy"):
            _note(conn, sid, "republished-copy", f"{s['key']}: {s['publisher']} - opened as a republished copy, original page not opened. {s['url']}", now)
        if s.get("note"):
            _note(conn, sid, "source-note", f"{s['key']}: {s['note']}", now)
        if s.get("is_archive_copy"):
            try:
                _archived_source(conn, sid, s, now, data_dir, get)
            except Exception as ex:  # noqa: BLE001
                log["sources"][s["key"]] = f"archive copy did not open: {type(ex).__name__}"
        else:
            rows = evidence.archive_story(conn, sid, now, data_dir, cfg, get=get, wb=wb or evidence.wayback, urls_override=[s["url"]], max_wayback=1)
            conn.execute("UPDATE evidence_archive SET issuer=?, published_at=? WHERE story_id=? AND url=?",
                         (s["publisher"], s.get("published") or None, sid, s["url"]))
            log["sources"][s["key"]] = f"HTTP {rows[0]['http_status']}, archive: {rows[0]['wayback_status'] or 'none'}"
    conn.commit()
    # 2. facts: UNVERIFIED until the real link check passes
    have = {r["claim_text"] for r in conn.execute("SELECT claim_text FROM claims WHERE story_id=?", (sid,))}
    for f in spec["facts"]:
        text = f["text"] + (f" [{f['attrib']}]" if f.get("attrib") else "")
        if text in have:
            continue
        s = srcs[f["src"]]
        conn.execute("INSERT INTO claims(story_id,claim_type,claim_text,status,manual,source_url,evidence,cite_text,checked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (sid, f.get("type", "fact"), text, "UNVERIFIED", 1, s["url"], f"fact sheet {f['src']}" + (f" (also {f['also']})" if f.get("also") else ""),
                      f["cite"], iso(now)))
        log["facts_added"] += 1
    # 3. notes: never facts
    for kind, items in (spec.get("notes") or {}).items():
        for t in items:
            _note(conn, sid, kind, t, now)
            log["notes_added"] += 1
    conn.commit()
    # 4. the REAL link check (opens every cited link, checks the cited text and the archived copy)
    res = linkcheck.run(conn, sid, now, data_dir, cfg, get=get, wb=wb or evidence.wayback)
    claims = {c["id"]: c for c in linkcheck.claims_with_urls(conn, sid)}
    passed = 0
    for row in res["rows"]:
        c = claims[row["claim_id"]]
        key = next((k for k, s in srcs.items() if s["url"] == c["source_url"]), None)
        if row["found"] and row["status"] in (200, "200"):
            kind = srcs[key].get("kind", "press") if key else "press"
            conn.execute("UPDATE claims SET status=?, evidence=?, checked_at=? WHERE id=? AND status='UNVERIFIED'",
                         (STATUS_BY_KIND[kind], f"{c['evidence']}; cited text found on the opened page {iso(now)[:10]}", iso(now), c["id"]))
            passed += 1
    conn.commit()
    changelog.seal(conn)
    log.update(link_check_ok=res["ok"], rows=len(res["rows"]), text_found=passed,
               failed=[{"claim_id": r["claim_id"], "url": r["url"][:70], "why": "; ".join(r["reasons"])} for r in res["rows"] if not r["ok"]],
               unmatched=[r["claim_id"] for r in res["rows"] if not r["found"]])
    return log
