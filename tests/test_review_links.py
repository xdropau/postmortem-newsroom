"""Source-link integrity, the five-point checklist, one-tap buttons with Undo, live updates, the test card and the website feed."""
import json
from datetime import timedelta

import pytest

from conftest import NOW, FakeWeb, Resp
from newsroom import commands, cycle, editorial as ed, linkcheck as lc, review, siteexport as se
from newsroom import secondcheck as sc
from newsroom.fetch import FetchError

URL = "https://www.sec.gov/doc1"
HEAD = "Regulator charges bank over fraud scheme"
PAGE = (b"<html><body><p>Regulator charges bank over fraud scheme.</p><p>The regulator said the bank misled investors about the "
        b"quality of its loan book across three consecutive years.</p></body></html>")
WB = lambda u: ("https://web.archive.org/web/2026/" + u, "captured now")   # noqa: E731
NOWB = lambda u: ("", "no capture")                                          # noqa: E731


def mk(conn, sid=1, headline=HEAD, url=URL, status="ready", claim_type="core", second=True):
    conn.execute("INSERT INTO stories(id,headline,headline_norm,source_org,original_url,primary_url,published_at,pub_known,detected_at,last_item_at,category,score,evidence_ok,editorial_status) "
                 "VALUES(?,?,?,?,?,?,?,1,?,?,?,90,1,?)", (sid, headline, headline.lower(), "SEC", url, url, "2026-11-24T19:00:00Z",
                                                          "2026-11-24T19:10:00Z", "2026-11-24T19:10:00Z", "Financial crime", status))
    conn.execute("INSERT INTO items(story_id,source_id,org,bot,tier,title,url,canonical_url,published_at,pub_known,detected_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                 (sid, "sec", "SEC", "primary", "primary", headline, url, url, "2026-11-24T19:00:00Z", "2026-11-24T19:10:00Z"))
    conn.execute("INSERT INTO claims(story_id,claim_type,claim_text,status,source_url) VALUES(?,?,?,?,?)", (sid, claim_type, "Bank charged over fraud scheme", "VERIFIED", url))
    conn.commit()
    if second:
        sc.store(conn, sid, [sc._item("headline", "h", "PASS", "x")], NOW)


@pytest.fixture
def web():
    return FakeWeb({URL: Resp(PAGE)})


@pytest.fixture
def db(conn):
    mk(conn)
    return conn


# ---------------------------------------------------------------- link check
def test_coverage_helper():
    assert lc.coverage("Regulator charges bank", "Today the Regulator charges bank again") == 1.0     # short text must match as a phrase
    assert lc.coverage("Regulator charges bank", "the regulator charges the bank today") == 0.0
    assert lc.coverage("Regulator charges bank over fraud scheme", "the bank was charged over a fraud scheme by the regulator") >= 0.8
    assert lc.coverage("Regulator charges bank over fraud scheme", "completely different text about cats") < 0.8
    assert lc.coverage("", "anything") == 1.0


def test_link_check_passes_and_records_a_complete_source_record(db, web, tmp_path):
    res = lc.run(db, 1, NOW, str(tmp_path), get=web, wb=WB)
    assert res["ok"]
    rec = lc.record_for(db, 1, URL)
    assert rec["issuer"] == "SEC" and rec["published_at"] and rec["retrieved_at"] and (rec["wayback_url"] or rec["full_copy_path"])
    assert lc.gate(db, 1, NOW)[0]


def test_cited_text_missing_blocks_approval(db, tmp_path):
    web = FakeWeb({URL: Resp(b"<html><body><p>This page is now about something entirely different, such as gardening tips.</p></body></html>")})
    res = lc.run(db, 1, NOW, str(tmp_path), get=web, wb=WB)
    assert not res["ok"] and "the cited text is not on the page" in res["rows"][0]["reasons"]
    ed.save_version(db, 1, "article", "Body", NOW)
    r = ed.approve(db, 1, NOW)
    assert not r["ok"] and "links not verified" in r["error"]


def test_dead_link_and_missing_archive_block_approval(db, tmp_path):
    dead = FakeWeb({URL: FetchError("HTTP 404", 404)})
    res = lc.run(db, 1, NOW, str(tmp_path), get=dead, wb=WB)
    assert not res["ok"] and "the page did not open (HTTP 404)" in res["rows"][0]["reasons"]
    mk(db, 2, headline="Other story about a company", url="https://news.example.com/a")
    res2 = lc.run(db, 2, NOW, str(tmp_path), get=FakeWeb({"https://news.example.com/a": Resp(PAGE.replace(b"Regulator charges bank over fraud scheme", b"Other story about a company"))}), wb=NOWB)
    assert not res2["ok"] and "no archived copy" in res2["rows"][0]["reasons"]


def test_claim_without_a_source_cannot_be_approved(db, web, tmp_path):
    db.execute("INSERT INTO claims(story_id,claim_type,claim_text,status) VALUES(1,'figure','A figure with no source','VERIFIED')")
    db.commit()
    ok, why = lc.gate(db, 1, NOW)
    assert not ok and "no source link" in why
    lc.run(db, 1, NOW, str(tmp_path), get=web, wb=WB)
    assert not lc.gate(db, 1, NOW)[0]                          # still blocked: that fact has no link
    ed.save_version(db, 1, "article", "Body", NOW)
    assert "no source link" in ed.approve(db, 1, NOW)["error"] or "links not verified" in ed.approve(db, 1, NOW)["error"]


def test_changing_claims_after_the_check_invalidates_it(db, web, tmp_path):
    lc.run(db, 1, NOW, str(tmp_path), get=web, wb=WB)
    assert lc.gate(db, 1, NOW)[0]
    db.execute("INSERT INTO claims(story_id,claim_type,claim_text,status,source_url) VALUES(1,'figure','New figure','VERIFIED',?)", (URL,))
    db.commit()
    assert "facts changed" in lc.gate(db, 1, NOW)[1]


def test_old_check_is_stale_and_manual_override_works(db, tmp_path):
    forbidden = FakeWeb({URL: FetchError("HTTP 403", 403)})
    assert not lc.run(db, 1, NOW, str(tmp_path), get=forbidden, wb=WB)["ok"]
    assert not lc.resolve(db, 1, 1, "ok", NOW)                  # a real note is required
    assert lc.resolve(db, 1, 1, "opened it in my browser, text is there", NOW)
    res = lc.run(db, 1, NOW, str(tmp_path), get=forbidden, wb=WB)
    assert res["ok"] and "manually verified" in db.execute("SELECT reason FROM link_checks ORDER BY id DESC LIMIT 1").fetchone()["reason"]
    assert not lc.gate(db, 1, NOW + timedelta(hours=2))[0]      # a pass older than an hour is stale


# ---------------------------------------------------------------- weekly re-check
def test_weekly_alerts_name_story_and_link_and_dedupe(db, web, tg, tmp_path):
    n, t = tg
    lc.run(db, 1, NOW, str(tmp_path), get=web, wb=WB)
    db.execute("UPDATE stories SET editorial_status='published' WHERE id=1")
    db.commit()
    ok = lc.weekly(db, n, NOW + timedelta(days=7), str(tmp_path), get=web)
    assert ok["problems"] == 0 and any("all still open and unchanged" in m for m in t.sent) and not any("LINK PROBLEM" in m for m in t.sent)
    t.sent.clear()
    dead = FakeWeb({URL: FetchError("HTTP 404", 404)})
    r = lc.weekly(db, n, NOW + timedelta(days=14), str(tmp_path), get=dead)
    assert r["problems"] == 1 and r["alerts"] == 1
    msg = next(m for m in t.sent if "LINK PROBLEM" in m)
    assert "PM-000001" in msg and HEAD in msg and URL in msg and "did not open" in msg
    t.sent.clear()
    assert lc.weekly(db, n, NOW + timedelta(days=14, hours=1), str(tmp_path), get=dead)["alerts"] == 0   # same week: no repeat alert


def test_weekly_detects_changed_source_page(db, web, tg, tmp_path):
    n, t = tg
    lc.run(db, 1, NOW, str(tmp_path), get=web, wb=WB)
    db.execute("UPDATE stories SET editorial_status='approved' WHERE id=1")
    db.commit()
    same_headline_new_body = FakeWeb({URL: Resp(b"<html><body><p>Regulator charges bank over fraud scheme.</p><p>Update: all charges were withdrawn after a "
                                                 b"court ruling, and the earlier statement has been removed from this page.</p></body></html>")})
    r = lc.weekly(db, n, NOW + timedelta(days=7), str(tmp_path), get=same_headline_new_body)
    assert r["problems"] == 1 and any("has changed since we archived it" in m for m in t.sent)


# ---------------------------------------------------------------- checklist and buttons
def test_checklist_lists_five_checks_and_one_missing_line(db):
    cl = review.checklist(db, 1)
    assert set(cl["flags"]) == {"sources", "facts", "dates", "second", "links"}
    assert cl["flags"]["links"] is False and not cl["all_ok"]
    txt = review.checklist_text(cl)
    assert "Links verified: no" in txt and "Sources found: yes" in txt and "Missing:" in txt and "Needs more checking" in txt


def test_callback_data_fits_telegram_limit(db):
    for row in review.keyboard(db, 1)["inline_keyboard"]:
        for b in row:
            assert len(b["callback_data"].encode()) <= 64
    labels = [b["text"] for row in review.keyboard(db, 1)["inline_keyboard"] for b in row]
    assert any("Approve" in l for l in labels) and any("Needs more checking" in l for l in labels) and any("Reject" in l for l in labels)
    assert any("BREAKING" in l for l in labels) and any("LIVE" in l for l in labels)


def test_approve_tap_runs_the_link_check_then_approves_and_undo_works(db, web, tmp_path):
    r = review.apply_action(db, "approve", 1, NOW, str(tmp_path), get=web, wb=WB)
    assert "Approved" in r["text"] and r["site_dirty"]
    assert ed.can_publish(db, 1, "website")[0] and db.execute("SELECT editorial_status e FROM stories").fetchone()["e"] == "approved"
    tok = int(r["markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2])
    u = review.apply_action(db, "undo", tok, NOW + timedelta(minutes=5))
    assert "Undone" in u["text"] and not ed.can_publish(db, 1, "website")[0]
    assert db.execute("SELECT editorial_status e FROM stories").fetchone()["e"] == "ready"
    assert "Already undone" in review.apply_action(db, "undo", tok, NOW + timedelta(minutes=6))["text"]


def test_undo_expires_after_ten_minutes(db, web, tmp_path):
    r = review.apply_action(db, "approve", 1, NOW, str(tmp_path), get=web, wb=WB)
    tok = int(r["markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2])
    late = review.apply_action(db, "undo", tok, NOW + timedelta(minutes=11))
    assert "Too late" in late["text"] and ed.can_publish(db, 1, "website")[0]


def test_approve_is_blocked_when_a_link_fails_and_says_why(db, tmp_path):
    dead = FakeWeb({URL: FetchError("HTTP 404", 404)})
    r = review.apply_action(db, "approve", 1, NOW, str(tmp_path), get=dead, wb=WB)
    assert "Cannot approve yet" in r["text"] and "Links verified: no" in r["text"] and not r["site_dirty"]
    assert db.execute("SELECT COUNT(*) c FROM approvals WHERE action='approve'").fetchone()["c"] == 0


def test_approve_blocked_when_a_claim_is_unverified_or_date_unknown(db, web, tmp_path):
    db.execute("INSERT INTO claims(story_id,claim_type,claim_text,status,source_url) VALUES(1,'figure','x','UNVERIFIED',?)", (URL,))
    db.execute("UPDATE stories SET pub_known=0")
    db.commit()
    r = review.apply_action(db, "approve", 1, NOW, str(tmp_path), get=web, wb=WB)
    assert "Cannot approve yet" in r["text"] and "Facts checked: no" in r["text"] and "Dates confirmed: no" in r["text"]


def test_reject_and_undo(db):
    r = review.apply_action(db, "reject", 1, NOW)
    assert "Rejected" in r["text"] and db.execute("SELECT editorial_status e FROM stories").fetchone()["e"] == "rejected"
    tok = int(r["markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2])
    review.apply_action(db, "undo", tok, NOW + timedelta(minutes=2))
    assert db.execute("SELECT editorial_status e FROM stories").fetchone()["e"] == "ready"


def test_breaking_banner_only_after_approval_and_undo(db, web, tmp_path):
    b = review.apply_action(db, "breaking", 1, NOW)
    assert "once you approve" in b["text"] and se.build(db, NOW)["breaking"] is None
    review.apply_action(db, "approve", 1, NOW, str(tmp_path), get=web, wb=WB)
    assert se.build(db, NOW)["breaking"]["story_id"] == "PM-000001"
    tok = int(b["markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2])
    review.apply_action(db, "undo", tok, NOW + timedelta(minutes=1))
    assert se.build(db, NOW)["breaking"] is None


def test_live_start_update_cards_and_approval(db, tg):
    n, t = tg
    review.apply_action(db, "live", 1, NOW)
    ed.discover_live_updates(db, NOW)
    assert review.send_live_update_cards(db, n, NOW) == 1
    assert "LIVE update to approve" in t.sent[-1] and t.markups[-1]["inline_keyboard"][0][0]["callback_data"].startswith("pm:lvapprove:")
    uid = db.execute("SELECT id FROM live_updates").fetchone()["id"]
    assert se.build(db, NOW)["live"] == []                      # draft is not public
    r = review.apply_action(db, "lvapprove", uid, NOW)
    assert "Update approved" in r["text"] and len(se.build(db, NOW)["live"][0]["updates"]) == 1
    tok = int(r["markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2])
    review.apply_action(db, "undo", tok, NOW + timedelta(minutes=1))
    assert se.build(db, NOW)["live"] == []
    assert review.send_live_update_cards(db, n, NOW) == 0       # no repeat card for the same update


def test_live_update_skip_hides_it(db):
    review.apply_action(db, "live", 1, NOW)
    ed.discover_live_updates(db, NOW)
    uid = db.execute("SELECT id FROM live_updates").fetchone()["id"]
    review.apply_action(db, "lvskip", uid, NOW)
    assert se.build(db, NOW)["live"] == [] and ed.discover_live_updates(db, NOW) == 0


# ---------------------------------------------------------------- taps through Telegram
def test_button_taps_come_only_from_the_owner_and_edit_the_card(db, tg):
    n, t = tg
    t.updates = [{"update_id": 1, "callback_query": {"id": "c1", "data": "pm:reject:1", "message": {"chat": {"id": 999}, "message_id": 5}}},
                 {"update_id": 2, "callback_query": {"id": "c2", "data": "pm:reject:1", "message": {"chat": {"id": 42}, "message_id": 6}}}]
    assert commands.poll(db, n, NOW) == 1
    assert len(t.edits) == 1 and t.edits[0]["message_id"] == 6 and "Rejected" in t.edits[0]["text"]
    assert len(t.answers) == 1 and t.answers[0]["callback_query_id"] == "c2"
    assert db.execute("SELECT editorial_status e FROM stories").fetchone()["e"] == "rejected"


def test_garbage_button_data_is_ignored(db, tg):
    n, t = tg
    t.updates = [{"update_id": 1, "callback_query": {"id": "c1", "data": "evil:drop:1", "message": {"chat": {"id": 42}, "message_id": 6}}}]
    assert commands.poll(db, n, NOW) == 0 and db.execute("SELECT editorial_status e FROM stories").fetchone()["e"] == "ready"


def test_story_command_sends_card_with_buttons(db, tg):
    n, t = tg
    t.updates = [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/story 1"}}]
    commands.poll(db, n, NOW)
    assert "Checklist" in t.sent[-1] and t.markups[-1]["inline_keyboard"]


def test_alert_card_has_checklist_and_buttons(conn, cfg, tg, tmp_path):
    from newsroom.ingest import ingest, rescore_recent
    from test_pipeline import item, BIG
    n, t = tg
    ingest(conn, [item(BIG, "https://doj.gov/p1")], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    web = FakeWeb({"https://doj.gov/p1": Resp(b"<html>" + b"x" * 300 + b" $1.2 billion indicted Sydney</html>")})
    assert cycle.send_alerts(conn, n, cfg, NOW, get=web)
    assert "Checklist" in t.sent[0] and "Links verified" in t.sent[0] and t.markups[0] is not None


# ---------------------------------------------------------------- Telegram test card
def test_test_card_is_safe_scripted_and_never_exported(conn, tg):
    n, t = tg
    assert review.send_test_card(conn, n, NOW) == "sent"
    assert "TEST CARD" in t.sent[0] and len(t.markups[0]["inline_keyboard"]) == 3
    sid = review.ensure_test_card(conn, NOW)
    blocked = review.apply_action(conn, "approve", sid, NOW)
    assert "Cannot approve yet" in blocked["text"] and "Links verified: no" in blocked["text"]
    more = review.apply_action(conn, "more", sid, NOW)
    assert "Links verified: yes" in more["text"]
    ok = review.apply_action(conn, "approve", sid, NOW)
    assert "TEST: approved" in ok["text"] and "nothing was published" in ok["text"]
    assert se.build(conn, NOW)["stories"] == [] and not conn.execute("SELECT 1 FROM approvals").fetchone()
    from newsroom.cards import top_stories
    assert top_stories(conn, NOW) == []


# ---------------------------------------------------------------- briefing
def test_briefing_has_review_buttons(db, tg, cfg):
    from newsroom import briefing
    n, t = tg
    db.execute("UPDATE stories SET last_item_at=?, detected_at=?, published_at=?", ("2026-11-24T19:30:00Z",) * 3)
    db.commit()
    assert briefing.maybe_send(db, cfg, n, NOW) == "sent"
    kb = t.markups[-1]["inline_keyboard"][0]
    assert kb[0]["text"] == "Review 1" and kb[0]["callback_data"] == "pm:card:1" and "Checks:" in t.sent[-1]


# ---------------------------------------------------------------- website feed comes from source records
def test_site_feed_links_come_only_from_source_records(db, web, tmp_path):
    db.execute("INSERT INTO claims(story_id,claim_type,claim_text,status,source_url) VALUES(1,'names','Names are correct','VERIFIED',?)", (URL,))
    db.execute("INSERT INTO items(story_id,source_id,org,bot,tier,title,url,canonical_url,published_at,pub_known,detected_at) VALUES(1,'x','Blog','radar','outlet','Hand typed link','https://untracked.example.com/x','https://untracked.example.com/x','2026-11-24T19:05:00Z',1,'2026-11-24T19:10:00Z')")
    db.commit()
    review.apply_action(db, "approve", 1, NOW, str(tmp_path), get=web, wb=WB)
    s = se.build(db, NOW)["stories"][0]
    src = s["sources"][0]
    assert src["id"].startswith("SRC-") and src["publisher"] == "SEC" and src["published_at"] and src["retrieved_at"] and src["archive_url"].startswith("https://web.archive.org")
    assert src["archived_copy"] and src["link_verified_at"]
    assert [x["url"] for x in s["sources"]] == [URL]                        # one record per cited URL, nothing else
    assert all(t["url"] in ("", URL) for t in s["timeline"])                # an uncited URL is never linked
    assert [f["text"] for f in s["key_facts"]] == ["Bank charged over fraud scheme"]   # housekeeping checks are not public facts
    assert s["id"] == "PM-000001" and se.build(db, NOW)["evidence"][0]["story_id"] == "PM-000001"


def test_tapwatch_answers_a_tap_at_once_and_starts_the_worker_without_confirming_updates():
    from newsroom import tapwatch

    class S:
        def __init__(self):
            self.calls = []
            self.ups = [{"update_id": 7, "callback_query": {"id": "cb1", "data": "pm:approve:5", "message": {"chat": {"id": 42}, "message_id": 9}}},
                        {"update_id": 8, "callback_query": {"id": "cb2", "data": "pm:approve:5", "message": {"chat": {"id": 999}}}}]

        def post(self, url, json=None, timeout=None):
            self.calls.append((url.rsplit("/", 1)[1], json))

            class R:
                status_code = 200
                def __init__(s, body): s.body = body
                def json(s): return s.body
            return R({"ok": True, "result": self.ups} if "getUpdates" in url else {"ok": True})
    s, seen, fired = S(), set(), []
    assert tapwatch.step(s, "T", "42", seen, lambda: fired.append(1)) == 1
    names = [c[0] for c in s.calls]
    assert names == ["getUpdates", "answerCallbackQuery", "sendMessage"]
    assert "offset" not in s.calls[0][1]                       # never confirms: the real worker still gets the tap
    assert s.calls[1][1]["callback_query_id"] == "cb1" and "Working" in s.calls[1][1]["text"]
    assert "Approve (story 5)" in s.calls[2][1]["text"] and fired == [1]
    assert tapwatch.step(s, "T", "42", seen, lambda: fired.append(1)) == 0 and fired == [1]    # same tap is not acknowledged twice; strangers ignored
