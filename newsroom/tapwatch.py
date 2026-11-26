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


def github_dispatch():
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    def go():
        if not (repo and tok):
            return False
        r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/taps.yml/dispatches",
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, json={"ref": os.environ.get("GITHUB_REF_NAME", "main")}, timeout=15)
        return r.status_code == 204
    return go


def main(minutes: float = 50, every: float = 2.0) -> int:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat):
        print("Telegram not configured", file=sys.stderr)
        return 0
    sess, seen, end, n = requests.Session(), set(), time.time() + minutes * 60, 0
    go = github_dispatch()
    while time.time() < end:
        n += step(sess, token, chat, seen, go)
        time.sleep(every)
    print(f"tapwatch finished; acknowledged {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(float(os.environ.get("TAPWATCH_MINUTES", "50"))))
