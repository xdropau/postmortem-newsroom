"""Editorial layer: version-bound approvals, corrections, leads, breaking expiry, live coverage, story links, metrics, evidence."""
from datetime import timedelta

import pytest

from conftest import NOW
from newsroom import editorial as ed
from newsroom import linkcheck as lc
from newsroom import secondcheck as sc
from newsroom.db import connect


@pytest.fixture
def db(tmp_path):
    c = connect(str(tmp_path))
    c.execute("INSERT INTO stories(headline,headline_norm,source_org,original_url,primary_url,published_at,pub_known,detected_at,last_item_at,category,score,editorial_status) "
              "VALUES('Regulator charges bank','regulator charges bank','SEC','https://sec.gov/a','https://sec.gov/a','2026-11-24T19:00:00Z',1,'2026-11-24T19:10:00Z','2026-11-24T19:10:00Z','Finance',85,'ready')")
    c.execute("INSERT INTO claims(story_id,claim_type,claim_text,status,source_url) VALUES(1,'source','Source opens','VERIFIED','https://sec.gov/a')")
    c.execute("INSERT INTO items(story_id,source_id,org,bot,tier,title,url,canonical_url,published_at,pub_known,detected_at) "
              "VALUES(1,'sec','SEC','primary','primary','Charges','https://sec.gov/a','https://sec.gov/a','2026-11-24T19:00:00Z',1,'2026-11-24T19:10:00Z')")
    c.commit()
    sc.store(c, 1, [sc._item("headline", "h", "PASS", "x")], NOW)   # a passing second check
    lc.store_pass(c, 1, NOW)
    return c


def status(c):
    return c.execute("SELECT editorial_status e FROM stories WHERE id=1").fetchone()["e"]


def test_approval_is_bound_to_version_and_edit_voids_it(db):
    assert ed.save_version(db, 1, "article", "Body one", NOW)["version"] == 1
    assert ed.approve(db, 1, NOW)["ok"] and status(db) == "approved"
    assert ed.can_publish(db, 1)[0]
    r = ed.save_version(db, 1, "article", "Body one, with a changed figure", NOW + timedelta(minutes=1))
    assert r["version"] == 2 and r["invalidated"] == 1
    assert status(db) == "ready"
    ok, why = ed.can_publish(db, 1)
    assert not ok and "not approved" in why
    assert not ed.mark_published(db, 1, NOW)["ok"]
    # re-saving identical text is not a change and does not touch approvals
    assert ed.approve(db, 1, NOW)["ok"]
    assert ed.save_version(db, 1, "article", "Body one, with a changed figure  \r\n", NOW)["changed"] is False
    assert ed.can_publish(db, 1)[0]
    assert ed.mark_published(db, 1, NOW)["ok"] and status(db) == "published"


def test_cannot_approve_with_unverified_claims_or_without_content(db):
    assert not ed.approve(db, 1, NOW)["ok"]                     # no content
    ed.save_version(db, 1, "narration", "x", NOW)
    db.execute("INSERT INTO claims(story_id,claim_type,claim_text,status) VALUES(1,'names','Names correct','UNVERIFIED')")
    db.commit()
    r = ed.approve(db, 1, NOW)
    assert not r["ok"] and r["error"] == "verification incomplete"
    assert not ed.can_publish(db, 1)[0]


def test_destination_specific_approval(db):
    ed.save_version(db, 1, "article", "A", NOW)
    ed.approve(db, 1, NOW, destination="website")
    assert ed.can_publish(db, 1, "website")[0]
    assert not ed.can_publish(db, 1, "youtube")[0]


def test_reject_records_history_and_voids_approvals(db):
    ed.save_version(db, 1, "article", "A", NOW)
    ed.approve(db, 1, NOW)
    ed.reject(db, 1, NOW, "defamation risk")
    assert status(db) == "rejected" and not ed.can_publish(db, 1)[0]
    acts = [r["action"] for r in db.execute("SELECT action FROM approvals ORDER BY id")]
    assert acts == ["approve", "reject"]


def test_corrections_leads_and_breaking_expiry(db):
    c = ed.add_correction(db, 1, "Figure corrected from $2m to $1.2m", NOW)
    assert c["ok"] and ed.close_correction(db, c["id"], NOW)
    lead = ed.add_lead(db, NOW, "Pattern of late filings", documents="SEC 8-K x3", alternatives="clerical delay")
    assert db.execute("SELECT originality FROM leads").fetchone()["originality"] == "NOT CHECKED"
    assert ed.set_lead(db, lead["id"], NOW, status="researching") and not ed.set_lead(db, lead["id"], NOW, status="bogus")
    ed.set_breaking(db, 1, NOW, hours=500)                     # capped at 24h
    until = db.execute("SELECT breaking_until b FROM stories").fetchone()["b"]
    assert until == (NOW + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ed.expire_breaking(db, NOW + timedelta(hours=1)) == 0
    assert ed.expire_breaking(db, NOW + timedelta(hours=25)) == 1
    assert db.execute("SELECT breaking b FROM stories").fetchone()["b"] == 0


def test_live_updates_need_individual_approval_and_edits_void_it(db):
    assert ed.live_open(db, 1, NOW)["ok"] and ed.live_open(db, 1, NOW)["existing"]       # one page per event
    u = ed.live_add_update(db, 1, "Court filing posted", NOW, "https://sec.gov/a")["id"]
    assert ed.live_publishable(db, 1) == []
    assert ed.live_approve_update(db, u, NOW)["ok"] and len(ed.live_publishable(db, 1)) == 1
    ed.live_edit_update(db, u, NOW, text="Court filing posted and amended")
    assert ed.live_publishable(db, 1) == []
    assert not ed.live_approve_update(db, ed.live_add_update(db, 1, "no source", NOW)["id"], NOW)["ok"]
    assert ed.live_state(db, 1, "paused", NOW) and not ed.live_state(db, 1, "weird", NOW)
    assert ed.discover_live_updates(db, NOW) == 0          # PAUSED: no discovery
    ed.live_state(db, 1, "LIVE", NOW)
    assert ed.discover_live_updates(db, NOW) == 1 and ed.discover_live_updates(db, NOW) == 0   # idempotent


def test_one_story_id_and_no_duplicate_article(db):
    assert ed.uid(1) == "PM-000001"
    assert ed.link_story(db, 1, "article", "gid://shopify/Article/1", NOW)["ok"]
    assert ed.link_story(db, 1, "article", "gid://shopify/Article/1", NOW)["ok"]         # same ref is idempotent
    assert not ed.link_story(db, 1, "article", "gid://shopify/Article/2", NOW)["ok"]     # a second article is refused
    assert ed.link_story(db, 1, "video", "abc123", NOW)["ok"] and ed.link_story(db, 1, "social:facebook", "p1", NOW)["ok"]


def test_latency_and_evidence_export(db, tmp_path):
    m = ed.latency_metrics(db)
    assert m["source_to_detection"][0] == {"org": "SEC", "n": 1, "median_min": 10.0, "p90_min": 10.0}
    out = ed.export_evidence(db, str(tmp_path / "ev"))
    assert out["rows"] == 1
    head = open(tmp_path / "ev" / "evidence_library.csv", encoding="utf8").read().splitlines()[0]
    assert head.startswith("story_id,title,issuer,date,url,relevance,classification")
    assert ed.evidence_rows(db, public_only=True) == []        # story is not approved, so not public


def test_narration_check_enforces_length_and_verbatim_closing():
    body = " ".join(["word"] * 180)
    assert ed.check_narration(body + "\n" + ed.CLOSING)["ok"]
    assert not ed.check_narration(" ".join(["w"] * 100) + "\n" + ed.CLOSING)["ok"]
    assert not ed.check_narration(body)["ok"]
    assert not ed.check_narration(body + "\n" + ed.CLOSING.replace("Thank you for watching.", "Thanks."))["ok"]
    assert any("AI" in f for f in ed.check_narration(body + " Anthropic wrote this\n" + ed.CLOSING)["flags"])


def test_bad_op_in_inbox_does_not_raise(db):
    res = ed.apply_ops(db, [{"op": "nope"}, {"op": "save_version", "args": {"story_id": 999, "kind": "article", "content": "x"}},
                            {"op": "save_version", "args": {"story_id": 1, "kind": "article", "content": "ok"}}], NOW)
    assert [r["ok"] for r in res] == [False, False, True]
