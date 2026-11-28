"""World / AI-cyber / social sources: config rules, Telegram web-preview reader, the 3-region audience boost, new keywords."""
import json
import re

import pytest

from conftest import NOW
from newsroom import fetch
from newsroom.ingest import ingest, rescore_recent
from test_pipeline import item


class Page:
    def __init__(self, text, url="https://t.me/s/example", history=()):
        self.content, self.status_code, self.url, self.history = text.encode(), 200, url, list(history)
        self.text = text


def _msg(i, text, when="2026-11-24T19:50:00+00:00"):
    return (f'<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="example/{i}">'
            f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
            f'<a class="tgme_widget_message_date" href="https://t.me/example/{i}"><time datetime="{when}"></time></a></div></div>')


def test_source_rules(cfg):
    ids = set()
    for s in cfg["sources"]["sources"]:
        assert s["id"] not in ids, s["id"]
        ids.add(s["id"])
        assert s.get("region"), f"{s['id']} has no region"
        assert (s["bot"], s["tier"]) in {("radar", "outlet"), ("primary", "primary"), ("trends", "trend")}, s["id"]
        if s.get("telegram_preview") or s["id"].startswith(("bsky-", "masto-", "tg-", "reddit-", "gtrends-")):
            assert s["bot"] == "trends" and s["tier"] == "trend", s["id"]        # social and trends are leads only: never alert alone
        if s.get("google_news"):
            assert s.get("headlines_only") or not s.get("enabled", True) or s["id"] in ("reuters-gn", "ap-gn", "accc-gn", "reuters-world-gn"), s["id"]
    on = {s["id"] for s in cfg["sources"]["sources"] if s.get("enabled", True)}
    assert {"cisa-advisories", "ncsc-uk", "pib-gn", "sebi-gn", "rbi", "mfa-china-gn"} <= on
    assert not any(s["id"] in on and "twitter" in s["url"].lower() or "nitter" in s["url"].lower() or "//x.com" in s["url"] for s in cfg["sources"]["sources"])
    prim = {s["id"]: s for s in cfg["sources"]["sources"]}
    for k in ("cisa-advisories", "pib-gn", "sebi-gn", "rbi", "mfa-china-gn", "ncsc-uk"):
        assert prim[k]["tier"] == "primary" and prim[k]["bot"] == "primary"         # government and regulator feeds count as primary
    for k in ("bleepingcomputer", "krebs", "openai-news", "the-hindu-gn", "bangkok-post"):
        assert prim[k]["tier"] == "outlet"                                          # secondary outlets and company blogs do not


def test_telegram_preview_reader():
    html = ("<html>" + _msg(1, "short") + _msg(2, "A long enough public channel message about a breaking development today") +
            _msg(3, "Another sufficiently long message with <b>markup</b> &amp; an entity in it, newest one")) + "</html>"
    src = {"id": "tg-example", "org": "Telegram Example", "bot": "trends", "tier": "trend", "url": "https://t.me/s/example", "telegram_preview": True}
    items, status = fetch.fetch_items(src, lambda url, **k: Page(html) if "robots" not in url else Page("User-agent: *\nAllow: /"))
    assert status == 200 and [i["url"] for i in items] == ["https://t.me/example/3", "https://t.me/example/2"]     # newest first, the short one skipped
    assert items[0]["title"].startswith("Another sufficiently long message with markup & an entity")
    assert items[0]["published_at"].isoformat().startswith("2026-11-24T19:50") and "telegram-preview" in items[0]["tags"]


def test_telegram_channel_without_preview_is_a_failure_not_worked_around():
    src = {"id": "tg-x", "org": "x", "bot": "trends", "tier": "trend", "url": "https://t.me/s/example", "telegram_preview": True}
    redirected = Page("<html>landing</html>", url="https://t.me/example", history=[object()])
    with pytest.raises(fetch.FetchError):
        fetch.fetch_items(src, lambda url, **k: redirected if "robots" not in url else Page(""))
    with pytest.raises(fetch.FetchError):
        fetch.fetch_items(src, lambda url, **k: Page("<html>no messages</html>") if "robots" not in url else Page(""))


def _aud(conn, cfg, sources):
    tails = ["regulators say", "regulator says", "watchdog says", "authorities say", "officials say"]          # distinct text, same story
    ingest(conn, [dict(item(f"Bank fined billion over fraud scandal, {tails[n]}", f"https://example.com/{sid}", org=sid, bot="radar", tier="outlet"), source_id=sid)
                  for n, sid in enumerate(sources)], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    row = json.loads(conn.execute("SELECT score_json FROM stories ORDER BY id DESC LIMIT 1").fetchone()["score_json"])
    return row["breakdown"]["audience"], row["regions"]


def test_three_regions_give_a_small_audience_boost(conn, cfg):
    two, r2 = _aud(conn, cfg, ["bbc-top", "straits-times"])
    assert r2 == ["Singapore", "United Kingdom"]
    three, r3 = _aud(conn, cfg, ["bbc-top", "straits-times", "the-hindu-gn"])      # a fresh database each time would be cleaner; the story is re-found by headline
    assert r3 == ["India", "Singapore", "United Kingdom"] and three == two + 3 and three <= 15


def test_global_and_unknown_regions_do_not_count(conn, cfg):
    aud, regs = _aud(conn, cfg, ["bbc-top", "straits-times", "reuters-gn", "bleepingcomputer"])      # two real regions + two "Global"
    assert regs == ["Singapore", "United Kingdom"]


def test_new_keywords_use_word_boundaries(cfg):
    from newsroom.score import _compiled
    cats = _compiled(cfg)["cats"]
    hit = lambda cat, text: any(p.search(text) for p in cats[cat])
    for text in ("cisa warns of a zero-day", "ncsc urges patching", "cert-in issues advisory for a data breach", "a supply chain attack on a vendor",
                 "state-sponsored hackers", "global outage hits banks"):
        assert hit("Cybersecurity", text), text
    for text in ("deepfake video of the minister", "ai regulation bill passes", "ai lawsuit filed", "model leak at the lab"):
        assert hit("Technology and AI", text), text
    assert not hit("Cybersecurity", "a certain certificate was certified")           # CERT / CISA / NCSC only as whole words
