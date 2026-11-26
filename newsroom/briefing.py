"""7:00 AM Sydney briefing: the top 5 only, one short block each, one pipeline line. Sent once per Sydney day
(tolerates late GitHub cron starts). Priority ALERTS are separate and remain score 80+ only."""
from __future__ import annotations

from .cards import pipeline_counts, top_stories
from .notify import e
from .util import SYDNEY


def build(conn, cfg, now) -> str:
    syd = now.astimezone(SYDNEY)
    L = [f"☀️ <b>THE POST-MORTEM</b> · {syd.strftime('%a %d %b, %H:%M')} Sydney", "", "<b>Top 5 today</b>"]
    top = top_stories(conn, now)
    for i, c in enumerate(top, 1):
        L += [f"<b>{i}. {e(c['headline'])}</b>",
              f"   Score {c['score']} · {e(c['category'])} · {e(c['age'])}",
              f"   {e(c['verification'])}" + (" · official source" if c["is_primary"] else ""),
              f"   {e(c['link'])}",
              f"   Open: {c['cmd_open']}", ""]
    if not top:
        L += ["Nothing new in the last 24 hours.", ""]
    p = pipeline_counts(conn)
    L.append(f"Pipeline: {p['new']} worth a look · {p['review']} need review · {p['ready']} ready · {p['approved']} approved")
    return "\n".join(L)


def maybe_send(conn, cfg, notifier, now, force=False) -> str:
    syd = now.astimezone(SYDNEY)
    if not force and not (cfg["settings"]["briefing_hour_sydney"] <= syd.hour < 12):
        return "not-due"
    key = f"briefing:{syd.date().isoformat()}"
    return notifier.send_once(key, "briefing", build(conn, cfg, now), now)


def sent_today(conn, now) -> bool:
    key = f"briefing:{now.astimezone(SYDNEY).date().isoformat()}"
    return bool(conn.execute("SELECT 1 FROM notifications WHERE key=? AND ok=1", (key,)).fetchone())


def catch_up(conn, cfg, notifier, now) -> str:
    """Safety net, called from the health job: from 07:30 Sydney, send today's briefing if none has gone out."""
    syd = now.astimezone(SYDNEY)
    if (syd.hour, syd.minute) < (7, 30) or syd.hour >= 12 or sent_today(conn, now):
        return "not-needed"
    return maybe_send(conn, cfg, notifier, now)
