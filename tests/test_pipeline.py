from datetime import timedelta

import pytest

from conftest import NOW, FakeTelegram, FakeWeb, Resp, rss
from newsroom import commands, health
from newsroom.cycle import run_cycle, run_feed_bot, send_alerts
from newsroom.fetch import FetchError, http_get, parse_feed
from newsroom.ingest import ingest, rescore_recent
from newsroom.notify import Notifier
from newsroom.util import canonical_url, iso, same_story

BIG = ("Former CEO indicted for $1.2 billion securities fraud and money laundering, "
       "DOJ documents unsealed in Sydney court filing")


def item(title, url, org="DOJ", bot="primary", tier="primary", pub=None, summary="", links=None):
    return {"title": title, "url": url, "summary": summary, "published_at": pub or NOW - timedelta(minutes=5),
            "org": org, "source_id": org, "bot": bot, "tier": tier, "has_image": False, "links": links or []}


# ---------- dedup / clustering ----------
def test_canonical_url_strips_tracking():
    assert canonical_url("http://www.Example.com/a/?utm_source=x&b=2&fbclid=z#frag") == "https://example.com/a?b=2"


def test_same_story_vs_different(cfg):
    assert same_story("Fed holds interest rates steady at 4.5 percent", "Federal Reserve holds interest rates steady at 4.5 percent")
    assert not same_story("Fed holds interest rates steady", "Fed cuts interest rates sharply")


def test_duplicate_url_and_content_suppressed(conn, cfg):
    a = item("Bank fined $5 million by regulator", "https://x.com/a?utm_source=tw", "ASIC", summary="same body")
    s1 = ingest(conn, [a], NOW, cfg)
    s2 = ingest(conn, [dict(a, url="https://www.x.com/a")], NOW, cfg)           # same canonical URL
    s3 = ingest(conn, [dict(a, url="https://y.com/other", org="Other")], NOW, cfg)  # syndicated copy, same content
    assert (s1["new"], s2["dup_url"], s3["dup_content"]) == (1, 1, 1)
    assert conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 1


def test_independent_outlets_cluster_into_one_story(conn, cfg):
    t = "Optus fined $100 million over triple zero outage after regulator investigation"
    ingest(conn, [item(t, "https://abc.net.au/1", "ABC", "radar", "outlet", summary="a"),
                  item(t + " says ACMA", "https://guardian.com/2", "Guardian", "radar", "outlet", summary="b")], NOW, cfg)
    assert conn.execute("SELECT COUNT(*) c FROM stories").fetchone()["c"] == 1
    rescore_recent(conn, NOW, cfg)
    s = conn.execute("SELECT * FROM stories").fetchone()
    assert s["evidence_ok"] == 1


# ---------- scoring / alert gate ----------
def test_priority_story_scores_80_plus_and_alerts_once(conn, cfg, tg):
    n, t = tg
    web = FakeWeb({"https://doj.gov/p1": Resp(b"<html>" + b"x" * 300 + b" $1.2 billion indicted Sydney</html>")})
    ingest(conn, [item(BIG, "https://doj.gov/p1")], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    s = conn.execute("SELECT * FROM stories").fetchone()
    assert s["score"] >= 80, s["score_json"]
    sent = send_alerts(conn, n, cfg, NOW, get=web)
    assert sent == [s["id"]] and len(t.sent) == 1
    assert "Time since publication" in t.sent[0] and "Sydney:" in t.sent[0] and "doj.gov/p1" in t.sent[0]
    assert send_alerts(conn, n, cfg, NOW, get=web) == [] and len(t.sent) == 1   # duplicate suppressed


def test_single_outlet_no_primary_cannot_alert(conn, cfg, tg):
    n, t = tg
    ingest(conn, [item("Shocking bombshell: CEO slams rival over $1 billion fraud scandal in Sydney", "https://tab.com/1", "Tabloid", "radar", "outlet")], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    s = conn.execute("SELECT * FROM stories").fetchone()
    assert s["evidence_ok"] == 0 and s["sensational"] == 1
    assert send_alerts(conn, n, cfg, NOW, get=FakeWeb()) == [] and t.sent == []


def test_old_story_is_not_fresh(conn, cfg):
    ingest(conn, [item(BIG, "https://doj.gov/old", pub=NOW - timedelta(days=3))], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    assert conn.execute("SELECT score FROM stories").fetchone()["score"] < 80


def test_trend_leads_never_alert_alone(conn, cfg):
    ingest(conn, [item(BIG, "https://reddit.com/x", "Reddit", "trends", "trend")], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    s = conn.execute("SELECT * FROM stories").fetchone()
    assert s["lead_only"] == 1 and s["score"] <= 60


# ---------- failure recovery ----------
def test_http_retries_then_succeeds_and_does_not_retry_403():
    sleeps, n = [], {"c": 0}

    class S:
        def get(self, url, headers=None, timeout=None):
            n["c"] += 1
            return Resp(b"ok", 503 if n["c"] < 3 else 200)
    r = http_get("https://x", session=S(), sleep=sleeps.append)
    assert r.status_code == 200 and n["c"] == 3 and sleeps[1] > sleeps[0]  # exponential backoff

    class F:
        calls = 0
        def get(self, url, headers=None, timeout=None):
            F.calls += 1
            return Resp(b"no", 403)
    with pytest.raises(FetchError):
        http_get("https://x", session=F(), sleep=lambda s: None)
    assert F.calls == 1


def test_one_bad_source_does_not_stop_others_and_recovers(conn, cfg):
    good = rss([{"title": "Regulator fines bank $20 million over AML failures in Australia", "url": "https://g.com/1", "pub": NOW}])
    cfg2 = {**cfg, "sources": {"sources": [
        {"id": "good", "org": "Good", "bot": "radar", "tier": "outlet", "url": "https://g/feed"},
        {"id": "bad", "org": "Bad", "bot": "radar", "tier": "outlet", "url": "https://b/feed"}]}}
    state = {"bad_ok": False}
    web = FakeWeb({"https://g/feed": Resp(good), "https://b/feed": lambda n: Resp(good) if state["bad_ok"] else FetchError("HTTP 500", 500)})
    r1 = run_feed_bot(conn, "radar", cfg2, NOW, web)
    assert r1["status"] == "partial" and r1["new"] == 1
    for _ in range(2):
        run_feed_bot(conn, "radar", cfg2, NOW, web)
    assert conn.execute("SELECT consecutive_failures c FROM source_health WHERE source_id='bad'").fetchone()["c"] == 3
    state["bad_ok"] = True
    run_feed_bot(conn, "radar", cfg2, NOW, web)
    assert conn.execute("SELECT consecutive_failures c FROM source_health WHERE source_id='bad'").fetchone()["c"] == 0


def test_all_sources_down_marks_run_error(conn, cfg):
    cfg2 = {**cfg, "sources": {"sources": [{"id": "b", "org": "B", "bot": "radar", "tier": "outlet", "url": "https://b/f"}]}}
    r = run_feed_bot(conn, "radar", cfg2, NOW, FakeWeb({"https://b/f": FetchError("HTTP 500", 500)}))
    assert r["status"] == "error"


def test_failed_telegram_send_is_retried_next_cycle(conn, cfg):
    t = FakeTelegram(fail_first=10)
    n = Notifier(conn, "T", "42", sleep=lambda s: None, session=t)
    ingest(conn, [item(BIG, "https://doj.gov/p1")], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    web = FakeWeb({"https://doj.gov/p1": Resp(b"<html>" + b"y" * 300 + b"</html>")})
    assert send_alerts(conn, n, cfg, NOW, web) == []          # Telegram down: nothing recorded as sent
    t.fail_first = 0
    assert len(send_alerts(conn, n, cfg, NOW, web)) == 1      # recovers automatically


# ---------- health monitor ----------
def test_health_alerts_are_deduplicated(conn, cfg, tg):
    n, t = tg
    conn.execute("INSERT INTO source_health(source_id,bot,consecutive_failures,last_error) VALUES('doj-press','primary',5,'HTTP 500')")
    conn.execute("INSERT INTO runs(bot,started_at,finished_at,status) VALUES('radar',?,?,'ok')", (iso(NOW - timedelta(hours=3)),) * 2)
    conn.commit()
    r1 = health.run_health(conn, cfg, n, NOW)
    r2 = health.run_health(conn, cfg, n, NOW + timedelta(minutes=30))
    kinds = {i["kind"] for i in r1["issues"]}
    assert {"source_down", "radar_stale"} <= kinds
    assert r1["alerts_sent"] == 2 and r2["alerts_sent"] == 0
    assert health.run_health(conn, cfg, n, NOW + timedelta(hours=7))["alerts_sent"] == 2  # re-alerts after cooldown


# ---------- feed parsing ----------
def test_parse_feed_filters_and_strips(cfg):
    xml = rss([{"title": "8-K - ACME (Item 5.02)", "url": "https://sec.gov/a", "pub": NOW, "desc": "<b>Item 5.02</b> departure"},
               {"title": "8-K - OTHER (Item 2.02)", "url": "https://sec.gov/b", "pub": NOW, "desc": "Item 2.02 results"}])
    out = parse_feed(xml, {"id": "s", "org": "SEC", "bot": "primary", "tier": "primary", "require_any": ["item 5.02"]})
    assert [o["url"] for o in out] == ["https://sec.gov/a"] and "<b>" not in out[0]["summary"]


def test_google_news_org_extracted():
    xml = rss([{"title": "Court rules on tariffs - Reuters", "url": "https://news.google.com/rss/articles/abc", "pub": NOW}])
    o = parse_feed(xml, {"id": "g", "org": "Reuters", "bot": "radar", "google_news": True})[0]
    assert o["title"] == "Court rules on tariffs" and o["org"] == "Reuters"


def test_future_timestamp_is_clamped(conn, cfg):
    ingest(conn, [item(BIG, "https://doj.gov/f", pub=NOW + timedelta(days=5))], NOW, cfg)
    assert conn.execute("SELECT published_at p FROM items").fetchone()["p"] == iso(NOW)


# ---------- commands ----------
def test_commands_only_from_owner_and_approval_gate(conn, cfg, tg):
    n, t = tg
    ingest(conn, [item(BIG, "https://doj.gov/p1")], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    web = FakeWeb({"https://doj.gov/p1": Resp(b"<html>" + b"z" * 300 + b"</html>")})
    send_alerts(conn, n, cfg, NOW, web)
    t.sent.clear()
    t.updates = [{"update_id": 1, "message": {"chat": {"id": 999}, "text": "/approve 1"}},
                 {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/approve 1"}}]
    assert commands.poll(conn, n, NOW) == 1
    assert "Cannot approve" in t.sent[0]
    assert conn.execute("SELECT editorial_status e FROM stories").fetchone()["e"] != "approved"
    # resolve every outstanding claim, then approval works
    for c in conn.execute("SELECT id FROM claims WHERE status='UNVERIFIED'").fetchall():
        commands.handle(conn, f"/verify 1 {c['id']} VERIFIED checked", NOW)
    assert conn.execute("SELECT editorial_status e FROM stories").fetchone()["e"] == "ready"
    assert "second check not run" in commands.handle(conn, "/approve 1", NOW)
    from newsroom import secondcheck as sc
    sc.store(conn, 1, [sc._item("headline", "h", "PASS", "x")], NOW)
    assert "no saved draft" in commands.handle(conn, "/approve 1", NOW)
    assert "Cannot mark published" in commands.handle(conn, "/published 1", NOW)
    from newsroom import editorial as ed
    ed.save_version(conn, 1, "article", "Draft article v1", NOW)
    assert "approved article v1" in commands.handle(conn, "/approve 1", NOW)
    assert "Cannot mark published" not in commands.handle(conn, "/published 1", NOW)
    assert conn.execute("SELECT publication_status p FROM stories").fetchone()["p"] == "published"


def test_untitled_social_posts_use_text_as_headline():
    xml = (b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title><item><link>https://bsky.app/profile/x/post/1</link>"
           b"<description>Regulator files charges against bank over $20 million fraud</description></item></channel></rss>")
    o = parse_feed(xml, {"id": "b", "org": "Bluesky @x", "bot": "trends", "tier": "trend"})
    assert o and o[0]["title"].startswith("Regulator files charges")
