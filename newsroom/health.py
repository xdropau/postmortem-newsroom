"""BOT 07b - operational health monitor. Alerts are de-duplicated per issue per cooldown window."""
from __future__ import annotations

from datetime import timedelta

from .cycle import record_run
from .notify import e
from .util import human_age, iso, parse_iso


def _bucket(now, hours):
    return int(now.timestamp() // (hours * 3600))


def check(conn, cfg, now) -> list[dict]:
    st = cfg["settings"]
    issues = []

    last_ok = conn.execute("SELECT finished_at FROM runs WHERE bot='radar' AND status IN ('ok','partial') ORDER BY id DESC LIMIT 1").fetchone()
    if not last_ok:
        if conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone():
            issues.append({"kind": "radar_stale", "subject": "radar", "text": "Breaking News Radar has no successful run recorded."})
    else:
        age = (now - parse_iso(last_ok["finished_at"])).total_seconds() / 60
        if age > st["stale_radar_minutes"]:
            issues.append({"kind": "radar_stale", "subject": "radar",
                           "text": f"Breaking News Radar has not succeeded for {human_age(age * 60)} (limit {st['stale_radar_minutes']} min). The scheduler may have stopped."})

    for r in conn.execute("SELECT * FROM source_health WHERE consecutive_failures>=?", (st["source_failure_alert_after"],)):
        issues.append({"kind": "source_down", "subject": r["source_id"],
                       "text": f"Source {r['source_id']} unavailable ({r['consecutive_failures']} failures in a row; last error: {r['last_error'] or 'n/a'}; last success {r['last_success'] or 'never'})."})

    for (bot,) in conn.execute("SELECT DISTINCT bot FROM runs WHERE bot NOT IN ('health')").fetchall():
        last3 = [x["status"] for x in conn.execute("SELECT status FROM runs WHERE bot=? ORDER BY id DESC LIMIT 3", (bot,))]
        if len(last3) == 3 and all(s == "error" for s in last3):
            issues.append({"kind": "job_failing", "subject": bot, "text": f"Scheduled job '{bot}' failed its last 3 runs."})

    newest = conn.execute("SELECT MAX(detected_at) m FROM stories").fetchone()["m"]
    if newest and last_ok:
        gap = (now - parse_iso(newest)).total_seconds() / 3600
        if gap > st["no_new_stories_hours"]:
            issues.append({"kind": "no_new_info", "subject": "pipeline",
                           "text": f"No new stories for {gap:.1f}h although jobs report success. The system may have stopped processing new information."})

    day = iso(now)[:10]
    lim = st["limits"]
    for svc, cap in (("telegram", lim["telegram_per_day"]), ("youtube_units", lim["youtube_units_per_day"]), ("llm", lim["llm_calls_per_day"])):
        row = conn.execute("SELECT calls FROM api_usage WHERE day=? AND service=?", (day, svc)).fetchone()
        if row and row["calls"] >= lim["warn_fraction"] * cap:
            issues.append({"kind": "quota", "subject": svc, "text": f"API quota for {svc} is at {row['calls']}/{cap} today."})
    return issues


def run_health(conn, cfg, notifier, now) -> dict:
    issues = check(conn, cfg, now)
    sent = 0
    for i in issues:
        key = f"ops:{i['kind']}:{i['subject']}:{_bucket(now, cfg['settings']['ops_alert_cooldown_hours'])}"
        if notifier.send_once(key, "ops", f"⚠️ <b>OPS ALERT</b>\n{e(i['text'])}", now) == "sent":
            sent += 1
    record_run(conn, "health", now, now, "ok", detail=f"issues={len(issues)} alerts_sent={sent}")
    return {"issues": issues, "alerts_sent": sent}
