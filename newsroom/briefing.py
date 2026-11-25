"""Consolidated overnight briefing, sent once per Sydney day from 07:00 (tolerates late GitHub cron starts)."""
from __future__ import annotations

from datetime import timedelta

from .notify import e
from .util import fmt_sydney, human_age, iso, parse_iso, SYDNEY


def build(conn, cfg, now) -> str:
    prev = conn.execute("SELECT sent_at FROM notifications WHERE kind='briefing' AND ok=1 ORDER BY id DESC LIMIT 1").fetchone()
    since = max(parse_iso(prev["sent_at"]), now - timedelta(hours=30)) if prev else now - timedelta(hours=24)
    rows = conn.execute("SELECT * FROM stories WHERE detected_at>=? AND lead_only=0 ORDER BY score DESC LIMIT 10", (iso(since),)).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM stories WHERE detected_at>=? AND lead_only=0", (iso(since),)).fetchone()["c"]
    alerts = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind='alert' AND sent_at>=?", (iso(since),)).fetchone()["c"]
    leads = conn.execute("SELECT headline FROM stories WHERE lead_only=1 AND detected_at>=? ORDER BY id DESC LIMIT 5", (iso(since),)).fetchall()
    states = {r["editorial_status"]: r["c"] for r in conn.execute("SELECT editorial_status, COUNT(*) c FROM stories WHERE lead_only=0 GROUP BY 1")}
    down = [r["source_id"] for r in conn.execute("SELECT source_id FROM source_health WHERE consecutive_failures>=3")]
    lr = conn.execute("SELECT finished_at FROM runs WHERE bot='radar' AND status IN ('ok','partial') ORDER BY id DESC LIMIT 1").fetchone()
    L = [f"☀️ <b>THE POST-MORTEM overnight briefing</b>", f"{fmt_sydney(now)}", "",
         f"New stories since last briefing: <b>{total}</b> | priority alerts sent: <b>{alerts}</b>", "", "<b>Top stories</b>"]
    for r in rows:
        link = r["primary_url"] or r["original_url"] or ""
        L.append(f"• [{r['score']}] {e(r['headline'])[:150]} — {e(r['category'])} — {e(r['verification_status'])}\n  {e(link)}  (#{r['id']})")
    if not rows:
        L.append("• none")
    if leads:
        L += ["", "<b>Trending leads (unverified)</b>"] + [f"• {e(x['headline'])[:100]}" for x in leads]
    L += ["", f"Pipeline: new {states.get('new', 0)}, verifying {states.get('verifying', 0)}, ready {states.get('ready', 0)}, approved {states.get('approved', 0)}"]
    L.append("Health: " + (f"sources down: {', '.join(down)}" if down else "all sources responding") +
             (f"; last radar success {human_age((now - parse_iso(lr['finished_at'])).total_seconds())} ago" if lr else "; radar has not run"))
    return "\n".join(L)


def maybe_send(conn, cfg, notifier, now, force=False) -> str:
    syd = now.astimezone(SYDNEY)
    if not force and not (cfg["settings"]["briefing_hour_sydney"] <= syd.hour < 12):
        return "not-due"
    key = f"briefing:{syd.date().isoformat()}"
    return notifier.send_once(key, "briefing", build(conn, cfg, now), now)
