"""Deduplication (canonical URL, content hash, headline similarity) and story clustering."""
from __future__ import annotations

import json
from datetime import timedelta

from .score import is_primary_domain, rescore_story
from .util import canonical_url, content_hash, iso, norm_title, same_story


def _find_story(conn, title: str, lead: bool, now, cfg) -> int | None:
    since = iso(now - timedelta(hours=cfg["settings"]["cluster_window_hours"]))
    thr = cfg["settings"]["cluster_similarity"]
    rows = conn.execute("SELECT id, headline, lead_only FROM stories WHERE last_item_at>=? AND lead_only=?",
                        (since, 1 if lead else 0)).fetchall()
    best, best_id = 0.0, None
    from .util import similarity
    for r in rows:
        if same_story(title, r["headline"], thr):
            s, _ = similarity(title, r["headline"])
            if s > best:
                best, best_id = s, r["id"]
    return best_id


def ingest(conn, items: list[dict], now, cfg) -> dict:
    stats = {"seen": 0, "new": 0, "dup_url": 0, "dup_content": 0, "new_stories": 0, "touched": set()}
    for it in items:
        stats["seen"] += 1
        cu = canonical_url(it["url"])
        if conn.execute("SELECT 1 FROM items WHERE canonical_url=?", (cu,)).fetchone():
            stats["dup_url"] += 1
            continue
        ch = content_hash(it["title"], it["summary"])
        if conn.execute("SELECT 1 FROM items WHERE content_hash=?", (ch,)).fetchone():
            stats["dup_content"] += 1
            continue
        lead = it["bot"] == "trends"
        sid = _find_story(conn, it["title"], lead, now, cfg)
        pub = it["published_at"]
        if pub and pub > now:  # clock skew / bad feed: never trust a future timestamp
            pub = now
        if sid is None:
            cur = conn.execute(
                "INSERT INTO stories(headline, headline_norm, detected_at, last_item_at, lead_only) VALUES(?,?,?,?,?)",
                (it["title"], norm_title(it["title"]), iso(now), iso(now), 1 if lead else 0))
            sid = cur.lastrowid
            stats["new_stories"] += 1
        plinks = [l for l in it.get("links", []) if is_primary_domain(l, cfg)]
        conn.execute(
            """INSERT INTO items(story_id, source_id, org, bot, tier, title, url, canonical_url, content_hash,
               published_at, pub_known, detected_at, summary, has_image, primary_links, tags)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, it["source_id"], it["org"], it["bot"], it["tier"], it["title"], it["url"], cu, ch,
             iso(pub), 1 if pub else 0, iso(now), it["summary"], int(it["has_image"]), json.dumps(plinks), it.get("tags", "")))
        conn.execute("UPDATE stories SET last_item_at=? WHERE id=?", (iso(now), sid))
        stats["new"] += 1
        stats["touched"].add(sid)
    conn.commit()
    return stats


def trend_terms(conn, now) -> set[str]:
    since = iso(now - timedelta(hours=24))
    terms = set()
    for r in conn.execute("SELECT headline_norm FROM stories WHERE lead_only=1 AND last_item_at>=?", (since,)):
        t = r["headline_norm"].strip()
        if len(t.split()) >= 2 or len(t) >= 6:
            terms.add(t)
    return terms


def rescore_recent(conn, now, cfg, only_ids=None, hours=48):
    terms = trend_terms(conn, now)
    if only_ids is None:
        since = iso(now - timedelta(hours=hours))
        only_ids = [r["id"] for r in conn.execute("SELECT id FROM stories WHERE last_item_at>=?", (since,))]
    for sid in only_ids:
        rescore_story(conn, sid, now, terms, cfg)
