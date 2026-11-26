"""Instant acknowledgement of Telegram button taps (no database, no secrets printed).

A short-lived loop (run by .github/workflows/tapwatch.yml) looks at Telegram every couple of seconds. When the owner taps a button or
sends a message it (1) answers the tap at once ("Working...") so the spinner stops, (2) sends a one-line "got it" message, and
(3) starts the `taps` workflow, which does the real work (checks, approval, website feed) and replies with the result.

It never confirms updates (no offset is sent), so the `taps` workflow still receives every tap through the normal poll.
Public GitHub repositories get unlimited free Actions minutes, so this costs nothing."""
from __future__ import annotations

import os
import sys
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"
ACK_TEXT = "Working... result in about a minute"


def _owner(update: dict, chat_id: str) -> bool:
    cq = update.get("callback_query")
    m = (cq or {}).get("message") or update.get("message") or {}
    return str((m.get("chat") or {}).get("id")) == str(chat_id)


def describe(update: dict) -> str:
    cq = update.get("callback_query")
    if cq:
        parts = (cq.get("data") or "").split(":")
        verbs = {"approve": "Approve", "more": "Needs more checking", "reject": "Reject", "breaking": "Mark BREAKING", "live": "Start LIVE", "undo": "Undo"}
        if len(parts) == 3 and parts[0] == "pm":
            return f"{verbs.get(parts[1], parts[1])} (story {parts[2]})"
        return "a button"
    return "your message"


def step(sess, token: str, chat_id: str, seen: set, dispatch) -> int:
    """One look at Telegram. Returns the number of new owner taps/messages acknowledged."""
    def call(method, payload):
        try:
            r = sess.post(API.format(token=token, method=method), json=payload, timeout=15)
            return r.json() if r.status_code == 200 else {"ok": False, "status": r.status_code}
        except (requests.RequestException, ValueError):
            return {"ok": False}
    res = call("getUpdates", {"timeout": 0, "allowed_updates": ["message", "callback_query"]})   # no offset: nothing is confirmed
    fresh = [u for u in (res.get("result") or []) if u["update_id"] not in seen and _owner(u, chat_id)]
    for u in (res.get("result") or []):
        seen.add(u["update_id"])
    if not fresh:
        return 0
    for u in fresh:
        cq = u.get("callback_query")
        if cq:
            call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": ACK_TEXT})
        call("sendMessage", {"chat_id": chat_id, "text": f"⏳ Got it: {describe(u)}. Working on it; the result will appear here in about a minute."})
    dispatch()
    return len(fresh)


def gh(method: str, path: str, **kw):
    tok = os.environ.get("GITHUB_TOKEN")
    return requests.request(method, f"https://api.github.com/repos/{os.environ.get('GITHUB_REPOSITORY')}/{path}", timeout=15,
                            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, **kw)


def watcher_state(now, request=None, stale_minutes: int = 10) -> dict:
    """{alive, idle_minutes}: alive when a tapwatch run is in progress, or queued behind one that just finished (handover)."""
    from .util import parse_iso
    request = request or (lambda: gh("GET", "actions/workflows/tapwatch.yml/runs?per_page=5").json().get("workflow_runs", []))
    runs = request()
    if any(r["status"] == "in_progress" for r in runs):
        return {"alive": True, "idle_minutes": 0}
    done = [r for r in runs if r["status"] == "completed"]
    last = max((parse_iso(r["updated_at"]) for r in done), default=None)
    idle = (now - last).total_seconds() / 60 if last else 9999
    queued = any(r["status"] in ("queued", "waiting", "pending") for r in runs)
    return {"alive": idle <= stale_minutes or (queued and idle <= stale_minutes * 2), "idle_minutes": round(idle)}


def ensure_alive(notifier, now, request=None, start=None) -> str:
    """Restart the watcher if it is not running, and tell the owner (once per hour) if it has been down for more than 10 minutes."""
    try:
        st = watcher_state(now, request)
    except Exception as ex:  # noqa: BLE001 - never break the host workflow
        return f"unknown ({type(ex).__name__})"
    if st["alive"]:
        return "alive"
    (start or (lambda: gh("POST", "actions/workflows/tapwatch.yml/dispatches", json={"ref": "main"})))()
    notifier.send_once(f"tapwatch-down:{now.strftime('%Y%m%d%H')}", "ops",
                       f"🛠 <b>Button watcher was down</b> for about {st['idle_minutes']} minutes. Taps may have been slow. I have restarted it now; "
                       "it checks again on every cycle and health run.", now)
    return "restarted"


def github_dispatch():
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    def go():
        if not (repo and tok):
            return False
        r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/taps.yml/dispatches",
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, json={"ref": os.environ.get("GITHUB_REF_NAME", "main")}, timeout=15)
        return r.status_code == 204
    return go


HEARTBEATS = (("cycle.yml", 20), ("health.yml", 50))      # workflow, minutes after which a missing scheduled run is started by hand


def heartbeat(now, request=None, start=None) -> list[str]:
    """GitHub's free scheduler often skips runs. This always-on watcher starts the cycle / health workflow itself when one is overdue."""
    from .util import parse_iso
    started = []
    for wf, limit in HEARTBEATS:
        try:
            runs = (request or (lambda w: gh("GET", f"actions/workflows/{w}/runs?per_page=1").json().get("workflow_runs", [])))(wf)
            overdue = not runs or (runs[0]["status"] == "completed" and (now - parse_iso(runs[0]["created_at"])).total_seconds() > limit * 60)
            if overdue:
                (start or (lambda w: gh("POST", f"actions/workflows/{w}/dispatches", json={"ref": "main"})))(wf)
                started.append(wf)
        except Exception:  # noqa: BLE001 - never stop the watcher
            continue
    return started


def main(minutes: float = 50, every: float = 2.0) -> int:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat):
        print("Telegram not configured", file=sys.stderr)
        return 0
    sess, seen, end, n = requests.Session(), set(), time.time() + minutes * 60, 0
    go = github_dispatch()
    waiting, last_beat = None, 0.0
    while time.time() < end:
        if time.time() - last_beat > 300:
            last_beat = time.time()
            from .util import utcnow
            heartbeat(utcnow())
        got = step(sess, token, chat, seen, go)
        n += got
        if got:
            waiting = time.time()
        elif waiting and time.time() - waiting > 90:       # the worker has not finished: say so and start it again (never silent)
            waiting = None
            try:
                runs = gh("GET", "actions/workflows/taps.yml/runs?per_page=1").json().get("workflow_runs", [])
                if not runs or runs[0]["status"] != "completed":
                    sess.post(API.format(token=token, method="sendMessage"), json={"chat_id": chat, "text": "⚠️ GitHub is slow to start the worker. Your tap is still queued and will be processed; no need to tap again."}, timeout=15)
                    go()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(every)
    print(f"tapwatch finished; acknowledged {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(float(os.environ.get("TAPWATCH_MINUTES", "50"))))
