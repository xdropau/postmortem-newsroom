import json
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from newsroom.config import load_all  # noqa: E402
from newsroom.db import connect  # noqa: E402
from newsroom.fetch import FetchError  # noqa: E402
from newsroom.notify import Notifier  # noqa: E402

NOW = datetime(2026, 11, 24, 20, 0, 0, tzinfo=timezone.utc)  # 07:00 AEDT 25 Nov


def rss(entries):
    items = ""
    for e in entries:
        pub = f"<pubDate>{format_datetime(e['pub'])}</pubDate>" if e.get("pub") else ""
        items += (f"<item><title>{e['title']}</title><link>{e['url']}</link>{pub}"
                  f"<description>{e.get('desc', '')}</description></item>")
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>{items}</channel></rss>".encode()


class Resp:
    def __init__(self, content=b"", status=200, text=None):
        self.content, self.status_code = content, status
        self.text = text if text is not None else content.decode("utf-8", "ignore")
        self.headers = {}

    def json(self):
        return json.loads(self.content)


class FakeWeb:
    """url -> Resp | Exception | callable(n_calls)"""
    def __init__(self, routes=None):
        self.routes, self.calls = routes or {}, {}

    def __call__(self, url, **kw):
        self.calls[url] = self.calls.get(url, 0) + 1
        r = self.routes.get(url)
        if callable(r):
            r = r(self.calls[url])
        if r is None:
            raise FetchError("HTTP 404", 404)
        if isinstance(r, Exception):
            raise r
        return r


class FakeTelegram:
    def __init__(self, fail_first=0):
        self.sent, self.fail_first, self.updates = [], fail_first, []
        self.markups, self.edits, self.answers = [], [], []

    def post(self, url, json=None, timeout=None):
        if "editMessageText" in url:
            self.edits.append(json)
            return Resp(b'{"ok": true}')
        if "answerCallbackQuery" in url:
            self.answers.append(json)
            return Resp(b'{"ok": true}')
        if "getUpdates" in url:
            ups, self.updates = self.updates, []
            return Resp(__import__("json").dumps({"ok": True, "result": ups}).encode())
        if self.fail_first > 0:
            self.fail_first -= 1
            return Resp(b"{}", 400)
        self.sent.append(json["text"])
        self.markups.append(json.get("reply_markup"))
        return Resp(b'{"ok": true}')


@pytest.fixture
def cfg():
    return load_all()


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path))


@pytest.fixture
def tg(conn):
    t = FakeTelegram()
    return Notifier(conn, token="T", chat_id="42", sleep=lambda s: None, session=t), t
