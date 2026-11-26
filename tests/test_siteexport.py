"""The website export must contain ONLY approved material."""
import json
from datetime import timedelta

import pytest

from conftest import NOW
from newsroom import editorial as ed, siteexport as se
from newsroom import linkcheck as lc
from newsroom import secondcheck as sc
from newsroom.db import connect


def add_story(c, sid, headline, status="ready"):
    c.execute("INSERT INTO stories(id,headline,headline_norm,source_org,original_url,primary_url,published_at,pub_known,detected_at,last_item_at,category,score,editorial_status) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, headline, headline.lower(), "SEC", f"https://sec.gov/{sid}", f"https://sec.gov/{sid}",
                                                    "2026-11-24T19:00:00Z", 1, "2026-11-24T19:10:00Z", "2026-11-24T19:10:00Z", "Financial crime", 85, status))
    c.execute("INSERT INTO claims(story_id,claim_type,claim_text,status,source_url) VALUES(?,?,?,?,?)", (sid, "source", "Source opens", "VERIFIED", f"https://sec.gov/{sid}"))
    c.execute("INSERT INTO items(story_id,source_id,org,bot,tier,title,url,canonical_url,published_at,pub_known,detected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (sid, "sec", "SEC", "primary", "primary", "Charges filed", f"https://sec.gov/{sid}", f"https://sec.gov/{sid}", "2026-11-24T19:00:00Z", 1, "2026-11-24T19:10:00Z"))
    c.commit()
    sc.store(c, sid, [sc._item("headline", "h", "PASS", "x")], NOW)
    lc.store_pass(c, sid, NOW)


@pytest.fixture
def db(tmp_path):
    c = connect(str(tmp_path))
    add_story(c, 1, "Approved story about a bank")
    add_story(c, 2, "Draft story nobody approved")
    ed.save_version(c, 1, "article", "A long enough opening paragraph that describes what the documents show about the bank.\n\nSecond paragraph.", NOW)
    ed.save_version(c, 2, "article", "SECRET DRAFT TEXT that must never be exported anywhere at all.", NOW)
    assert ed.approve(c, 1, NOW, destination="website")["ok"]
    return c


def test_only_approved_stories_are_exported(db):
    data = se.build(db, NOW)
    assert data["approved_only"] is True
    assert [s["id"] for s in data["stories"]] == ["PM-000001"]
    assert "SECRET DRAFT" not in json.dumps(data) and "nobody approved" not in json.dumps(data)


def test_editing_after_approval_removes_story_until_reapproved(db):
    ed.save_version(db, 1, "article", "Changed body that is now unapproved and long enough to count as a paragraph.", NOW + timedelta(minutes=1))
    assert se.build(db, NOW)["stories"] == []
    assert ed.approve(db, 1, NOW + timedelta(minutes=2), destination="website")["ok"]
    assert len(se.build(db, NOW)["stories"]) == 1


def test_approval_for_another_destination_does_not_export(tmp_path):
    c = connect(str(tmp_path))
    add_story(c, 1, "Video only story")
    ed.save_version(c, 1, "article", "Body text long enough to be a real paragraph for the summary.", NOW)
    assert ed.approve(c, 1, NOW, destination="youtube")["ok"]
    assert se.build(c, NOW)["stories"] == []


def test_breaking_banner_needs_approval_and_expires(db):
    ed.set_breaking(db, 2, NOW, hours=6)                      # unapproved story flagged breaking: no banner
    assert se.build(db, NOW)["breaking"] is None
    ed.set_breaking(db, 1, NOW, hours=6)
    assert se.build(db, NOW)["breaking"]["story_id"] == "PM-000001"
    assert se.build(db, NOW + timedelta(hours=7))["breaking"] is None


def test_live_updates_need_individual_approval(db):
    ed.live_open(db, 2, NOW)
    u1 = ed.live_add_update(db, 2, "unapproved update", NOW, "https://x.gov/1")["id"]
    assert se.build(db, NOW)["live"] == []                    # nothing approved: the coverage is invisible
    u2 = ed.live_add_update(db, 2, "approved update", NOW, "https://x.gov/2")["id"]
    ed.live_approve_update(db, u2, NOW)
    live = se.build(db, NOW)["live"]
    assert len(live) == 1 and [u["text"] for u in live[0]["updates"]] == ["approved update"]
    ed.live_edit_update(db, u2, NOW, text="edited after approval")   # material edit voids approval
    assert se.build(db, NOW)["live"] == []


def test_corrections_and_evidence_only_for_public_stories(db):
    c1 = ed.add_correction(db, 1, "Fixed a date.", NOW)["id"]
    c2 = ed.add_correction(db, 2, "Correction on a draft story.", NOW)["id"]
    ed.close_correction(db, c1, NOW)
    ed.close_correction(db, c2, NOW)
    open_one = ed.add_correction(db, 1, "Not yet published correction.", NOW)["id"]
    data = se.build(db, NOW)
    assert [c["text"] for c in data["corrections"]] == ["Fixed a date."]
    assert {e["story_id"] for e in data["evidence"]} == {"PM-000001"}
    assert open_one and "Not yet published" not in json.dumps(data)


def test_cli_writes_file(db, tmp_path, monkeypatch):
    out = tmp_path / "site"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from newsroom.__main__ import main
    assert main(["site-export", "--out", str(out)]) == 0
    assert json.load(open(out / "pm-newsroom.json", encoding="utf-8"))["approved_only"] is True
