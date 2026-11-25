"""Bots 01-03 (ingest) and alert dispatch. Every bot/source is isolated: one failure never stops the others."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from . import changelog, editorial
from .config import sources_for
from .db import bump_usage
from .fetch import FetchError, http_get, parse_feed
from .ingest import ingest, rescore_recent
from .notify import format_alert
from .package import build_package, make_hooks
from .util import iso
from .verify import open_sources, outstanding, refresh_status, seed_claims


def _env_tag() -> str:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github-actions:" + os.environ.get("GITHUB_EVENT_NAME", "unknown")
    return "local"


def record_run(conn, bot, started, now, status, seen=0, new=0, detail=""):
    conn.execute("INSERT INTO runs(bot,started_at,finished_at,status,items_seen,items_new,detail,env,run_ref) VALUES(?,?,?,?,?,?,?,?,?)",
                 (bot, iso(started), iso(now), status, seen, new, detail[:500], _env_tag(), os.environ.get("GITHUB_RUN_ID", "")))
    conn.commit()


def mark_source(conn, src, now, ok: bool, n=0, err=None, http=None):
    conn.execute("INSERT OR IGNORE INTO source_health(source_id, bot, consecutive_failures) VALUES(?,?,0)", (src["id"], src["bot"]))
    if ok:
        conn.execute("UPDATE source_health SET last_success=?, last_attempt=?, last_error=NULL, consecutive_failures=0, last_items=?, last_http=? WHERE source_id=?",
                     (iso(now), iso(now), n, http or 200, src["id"]))
    else:
        conn.execute("UPDATE source_health SET last_attempt=?, last_error=?, consecutive_failures=consecutive_failures+1, last_http=? WHERE source_id=?",
                     (iso(now), (err or "")[:200], http, src["id"]))
    conn.commit()


def youtube_items(conn, now, get=http_get) -> list[dict]:
    key = os.environ.get("YOUTUBE_API_KEY", "")
    if not key:
        return []
    url = ("https://www.googleapis.com/youtube/v3/videos?part=snippet&chart=mostPopular&regionCode=AU"
           f"&maxResults=25&key={key}")
    r = get(url)
    bump_usage(conn, "youtube_units", 1, iso(now)[:10])
    out = []
    for v in r.json().get("items", []):
        sn = v.get("snippet", {})
        out.append({"title": sn.get("title", "")[:300], "url": f"https://www.youtube.com/watch?v={v['id']}",
                    "summary": (sn.get("channelTitle", "") or "")[:200], "published_at": None, "org": "YouTube Trending AU",
                    "source_id": "youtube-trending-au", "bot": "trends", "tier": "trend", "has_image": True, "links": []})
    return out


def run_feed_bot(conn, bot: str, cfg: dict, now, get=http_get) -> dict:
    started = now
    sources = sources_for(cfg, bot)
    items, ok, fail = [], 0, 0

    def work(src):
        try:
            r = get(src["url"])
            return src, parse_feed(r.content, src), r.status_code, None
        except Exception as ex:  # noqa: BLE001 - isolate every source
            return src, None, getattr(ex, "status", None), ex

    # Fetch in parallel (network only); all database writes stay on this thread.
    with ThreadPoolExecutor(max_workers=8) as pool:
        for src, got, status, ex in pool.map(work, sources):
            if ex is None:
                mark_source(conn, src, now, True, len(got), http=status)
                items += got
                ok += 1
            else:
                mark_source(conn, src, now, False, err=f"{type(ex).__name__}: {ex}", http=status)
                fail += 1
    if bot == "trends":
        try:
            yt = youtube_items(conn, now, get)
            items += yt
        except Exception as ex:  # noqa: BLE001
            fail += 1
            conn.execute("INSERT OR IGNORE INTO source_health(source_id,bot,consecutive_failures) VALUES('youtube-trending-au','trends',0)")
            conn.execute("UPDATE source_health SET consecutive_failures=consecutive_failures+1,last_error=?,last_attempt=? WHERE source_id='youtube-trending-au'",
                         (str(ex)[:200], iso(now)))
            conn.commit()
    stats = ingest(conn, items, now, cfg)
    status = "error" if (sources and ok == 0) else ("partial" if fail else "ok")
    record_run(conn, bot, started, now, status, stats["seen"], stats["new"], f"sources ok={ok} failed={fail}")
    return {"bot": bot, "status": status, "ok": ok, "failed": fail, **{k: v for k, v in stats.items() if k != "touched"}}


def send_alerts(conn, notifier, cfg, now, get=http_get) -> list[int]:
    st = cfg["settings"]
    since = iso(now - timedelta(hours=st["alert_max_age_hours"]))
    rows = conn.execute(
        "SELECT id FROM stories WHERE score>=? AND evidence_ok=1 AND lead_only=0 AND editorial_status!='rejected' "
        "AND last_item_at>=? ORDER BY score DESC", (st["alert_threshold"], since)).fetchall()
    sent = []
    for r in rows:
        sid = r["id"]
        if conn.execute("SELECT 1 FROM notifications WHERE key=? AND ok=1", (f"alert:{sid}",)).fetchone():
            continue
        s = dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
        has_primary = bool(s["primary_url"])
        if s["sensational"] and not has_primary:
            continue
        if len(sent) >= st["max_alerts_per_cycle"]:
            break
        seed_claims(conn, sid, cfg, now)
        if not conn.execute("SELECT 1 FROM source_checks WHERE story_id=?", (sid,)).fetchone():
            open_sources(conn, sid, now, get=get)
        refresh_status(conn, sid)
        build_package(conn, sid, os.environ.get("DATA_DIR", "data"), now)
        s = dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
        items = [dict(x) for x in conn.execute("SELECT * FROM items WHERE story_id=? ORDER BY id", (sid,))]
        text = format_alert(s, items, outstanding(conn, sid), make_hooks(s["headline"], items[0]["summary"] or "")[0], now)
        res = notifier.send_once(f"alert:{sid}", "alert", text, now, story_id=sid)
        if res == "sent":
            sent.append(sid)
    return sent


def run_cycle(conn, cfg, notifier, now, get=http_get, data_dir="data") -> dict:
    os.environ["DATA_DIR"] = data_dir
    out = {"bots": []}
    for bot in ("radar", "primary", "trends"):
        try:
            out["bots"].append(run_feed_bot(conn, bot, cfg, now, get))
        except Exception as ex:  # noqa: BLE001
            record_run(conn, bot, now, now, "error", detail=f"{type(ex).__name__}: {ex}")
            out["bots"].append({"bot": bot, "status": "error"})
    rescore_recent(conn, now, cfg)
    try:  # editorial housekeeping must never stop the news bots
        out["inbox_applied"] = editorial.process_inbox(conn, data_dir, now)
        out["breaking_expired"] = editorial.expire_breaking(conn, now)
        out["live_drafts"] = editorial.discover_live_updates(conn, now)
    except Exception as ex:  # noqa: BLE001
        record_run(conn, "editorial", now, now, "error", detail=f"{type(ex).__name__}: {ex}")
    changelog.seal(conn)
    try:
        out["alerts"] = send_alerts(conn, notifier, cfg, now, get)
    except Exception as ex:  # noqa: BLE001
        record_run(conn, "alerts", now, now, "error", detail=f"{type(ex).__name__}: {ex}")
        out["alerts"] = []
    return out
