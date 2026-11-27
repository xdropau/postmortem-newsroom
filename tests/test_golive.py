"""Approve as one action: check -> publish -> verify -> one message."""
from conftest import NOW, FakeWeb, Resp
from newsroom import golive, review, shopify
from test_review_links import PAGE, URL, WB, mk

BODY = '<h2>What happened</h2><p>x</p><h2>Sources</h2><ol><li><a href="https://src.example/a">A</a></li></ol>'
ADDR = "https://thepostmortem.org/blogs/investigations/my-story"


def link(conn, sid=1):
    conn.execute("INSERT INTO story_links(story_id,kind,ref,created_at) VALUES(?,?,?,?)", (sid, "article_url", ADDR, "2026-11-24T00:00:00Z"))
    conn.commit()


def web(live_page):
    base = FakeWeb({URL: Resp(PAGE)})
    return lambda url, **kw: Resp(live_page.encode()) if url.startswith(ADDR) else base(url, **kw)


def test_blocked_check_gives_one_line_naming_the_item(conn, tmp_path):
    mk(conn, second=False)
    link(conn)
    r = golive.run(conn, 1, NOW, str(tmp_path), get=FakeWeb({URL: Resp(PAGE)}), publish=lambda: (True, ""), ensure=lambda c, s: {"ok": True, "body": BODY}, wait=lambda s: None, wb=WB)
    assert not r["ok"] and r["message"].startswith("FAILED at step 1: Second check passed is not ready") and r["message"].endswith("Send /golive 1 to retry.")
    assert "\n" not in r["message"]


def test_full_chain_is_live_only_when_every_section_and_source_is_on_the_page(conn, tmp_path, monkeypatch):
    mk(conn)
    link(conn)
    monkeypatch.setattr(shopify, "_client_from_env", lambda session=None: None)      # the old approve hook is a no-op here
    page = "<html><h2>What happened</h2><h2>Sources</h2><a href='https://src.example/a'>A</a> https://src.example/a</html>"
    g = web(page)
    calls = []
    r = golive.run(conn, 1, NOW, str(tmp_path), get=g, publish=lambda: (calls.append(1) or (True, "")), ensure=lambda c, s: {"ok": True, "body": BODY}, wait=lambda s: None, wb=WB)
    assert r["ok"] and r["message"] == "LIVE: " + ADDR and calls == [1]
    assert conn.execute("SELECT editorial_status FROM stories WHERE id=1").fetchone()[0] == "approved"
    # a retry does not approve twice
    n = conn.execute("SELECT COUNT(*) FROM approvals WHERE story_id=1").fetchone()[0]
    golive.run(conn, 1, NOW, str(tmp_path), get=g, publish=lambda: (True, ""), ensure=lambda c, s: {"ok": True, "body": BODY}, wait=lambda s: None, wb=WB)
    assert conn.execute("SELECT COUNT(*) FROM approvals WHERE story_id=1").fetchone()[0] == n


def test_missing_source_on_live_page_fails_at_step_3_and_publish_failure_at_step_2(conn, tmp_path, monkeypatch):
    mk(conn)
    link(conn)
    monkeypatch.setattr(shopify, "_client_from_env", lambda session=None: None)
    g = web("<html><h2>What happened</h2><h2>Sources</h2></html>")
    r = golive.run(conn, 1, NOW, str(tmp_path), get=g, publish=lambda: (True, ""), ensure=lambda c, s: {"ok": True, "body": BODY}, wait=lambda s: None, wb=WB)
    assert r["message"].startswith("FAILED at step 3: the live page is missing 0 section(s) and 1 source link(s)")
    r2 = golive.run(conn, 1, NOW, str(tmp_path), get=g, publish=lambda: (False, "x"), ensure=lambda c, s: {"ok": True, "body": BODY}, wait=lambda s: None, wb=WB)
    assert r2["message"].startswith("FAILED at step 2: the public feed could not be published")


def test_test_items_never_go_live(conn, tmp_path):
    tid = review.ensure_test_card(conn, NOW)
    r = golive.run(conn, tid, NOW, str(tmp_path), get=FakeWeb({}), publish=lambda: (True, ""), ensure=lambda c, s: {"ok": True, "body": ""}, wait=lambda s: None, wb=WB)
    assert r["message"].startswith("FAILED at step 1: this is a test item")
