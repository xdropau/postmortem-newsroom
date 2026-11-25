"""BOT 07 - Telegram notifications with duplicate suppression."""
from __future__ import annotations

import html
import os
import time

import requests

from .db import bump_usage
from .util import fmt_sydney, fmt_utc, human_age, iso, parse_iso

API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, conn, token=None, chat_id=None, sleep=time.sleep, session=None):
        self.conn = conn
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self.sleep = sleep
        self.s = session or requests

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, payload: dict, retries: int = 3):
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.s.post(API.format(token=self.token, method=method), json=payload, timeout=20)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    wait = 5
                    try:
                        wait = int(r.json().get("parameters", {}).get("retry_after", 5))
                    except Exception:  # noqa: BLE001
                        pass
                    self.sleep(min(wait, 30))
                elif r.status_code < 500:
                    return {"ok": False, "status": r.status_code}
                last = r.status_code
            except requests.RequestException as e:
                last = type(e).__name__
            self.sleep(2 * (2 ** attempt))
        return {"ok": False, "error": str(last)}

    def send_raw(self, text: str, now) -> bool:
        if not self.configured:
            return False
        ok = True
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
        for ch in chunks:
            res = self._call("sendMessage", {"chat_id": self.chat_id, "text": ch, "parse_mode": "HTML",
                                             "disable_web_page_preview": True})
            bump_usage(self.conn, "telegram", 1, iso(now)[:10])
            ok = ok and bool(res.get("ok"))
        return ok

    def send_once(self, key: str, kind: str, text: str, now, story_id=None) -> str:
        """Returns 'sent', 'duplicate', 'failed' or 'unconfigured'. A failed send is not recorded, so it retries next cycle."""
        if self.conn.execute("SELECT 1 FROM notifications WHERE key=? AND ok=1", (key,)).fetchone():
            return "duplicate"
        if not self.configured:
            return "unconfigured"
        ok = self.send_raw(text, now)
        if ok:
            self.conn.execute("INSERT OR REPLACE INTO notifications(key,kind,story_id,sent_at,ok,payload) VALUES(?,?,?,?,?,?)",
                              (key, kind, story_id, iso(now), 1, text[:2000]))
            self.conn.commit()
            return "sent"
        return "failed"


def e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def format_alert(story: dict, items: list[dict], claims_out: list[dict], hook: str, now) -> str:
    pub = parse_iso(story["published_at"])
    det = parse_iso(story["detected_at"])
    lines = [f"🚨 <b>PRIORITY ALERT</b> · score <b>{story['score']}/100</b>", f"<b>{e(story['headline'])}</b>", ""]
    if pub:
        lines += [f"Published: {fmt_utc(pub)}", f"Sydney: {fmt_sydney(pub)}",
                  f"Time since publication (at detection): {human_age((det - pub).total_seconds())}; now {human_age((now - pub).total_seconds())}"]
    else:
        lines += ["Published: unknown (feed gave no timestamp)", f"Detected (Sydney): {fmt_sydney(det)}"]
    lines.append(f"Primary source: {e(story['primary_url']) if story['primary_url'] else 'none found yet (outlet reporting only)'}")
    seen, rep = set(), []
    for i in items:
        if i["bot"] != "trends" and i["org"] not in seen and i["url"] != story["primary_url"]:
            seen.add(i["org"])
            rep.append(f"• {e(i['org'])}: {e(i['url'])}")
    lines.append("Independent reporting:" + ("" if rep else " none yet"))
    lines += rep[:4]
    lines += ["", f"Verification: <b>{e(story['verification_status'])}</b>"]
    if claims_out:
        lines.append("Outstanding:")
        lines += [f"• #{c['id']} {e(c['claim_text'])[:140]}" for c in claims_out[:6]]
        if len(claims_out) > 6:
            lines.append(f"• +{len(claims_out) - 6} more")
    lines += ["", f"Hook: {e(hook)}", f"Package: story #{story['id']} ({e(story['package_path'] or 'pending')}). Reply /story {story['id']}"]
    return "\n".join(lines)
