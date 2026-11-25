"""Second check, change log, permanent records, evidence archive, backups and restore. No network: FakeWeb only."""
import json
import os
import sqlite3
import tarfile
from datetime import date, timedelta

import pytest

from conftest import NOW, FakeWeb, Resp
from newsroom import backup, changelog, evidence, secondcheck as sc
from newsroom import editorial as ed
from newsroom.config import load_all
from newsroom.db import connect

URL = "https://www.sec.gov/news/press-release/2026-1"
THIRD = "https://example-news.com/story"
PAGE = ("<html><body>The SEC charged Acme Capital Management and its founder John Smith with defrauding investors of $1.2 billion. "
        "Smith pleaded guilty on 24 November 2026 and agreed to pay a $5 million penalty. Acme denied wrongdoing in a statement. "
        + "Filler text about the case. " * 20 + "</body></html>")


@pytest.fixture
def db(tmp_path):
    c = connect(str(tmp_path))
    c.execute("INSERT INTO stories(headline,headline_norm,source_org,original_url,primary_url,published_at,pub_known,detected_at,last_item_at,category,score,evidence_ok,editorial_status) "
              "VALUES('SEC charges John Smith over $1.2 billion fraud','x','SEC',?,?,'2026-11-24T10:00:00Z',1,'2026-11-24T10:05:00Z','2026-11-24T10:05:00Z','Finance',85,1,'new')", (URL, URL))
    c.execute("INSERT INTO items(story_id,source_id,org,bot,tier,title,url,canonical_url,published_at,pub_known,detected_at,summary) "
              "VALUES(1,'sec','SEC','primary','primary','t',?,?,'2026-11-24T10:00:00Z',1,'2026-11-24T10:05:00Z','The SEC charged Acme and John Smith over $1.2 billion')", (URL, URL))
    c.execute("INSERT INTO items(story_id,source_id,org,bot,tier,title,url,canonical_url,published_at,pub_known,detected_at,summary) "
              "VALUES(1,'n','Example News','radar','outlet','t',?,?,'2026-11-24T11:00:00Z',1,'2026-11-24T11:05:00Z','x')", (THIRD, THIRD))
    c.execute("INSERT INTO claims(story_id,claim_type,claim_text,status) VALUES(1,'source','Source opens','VERIFIED')")
    c.commit()
    return c


def web(page=PAGE):
    return FakeWeb({URL: Resp(page.encode()), THIRD: Resp(("<html>" + "Example outlet repeats it. " * 20 + "</html>").encode())})


def by(sm, key):
    return [i for i in sm["items"] if i["key"] == key]


# ---------------------------------------------------------------- second check
def test_second_check_passes_on_supportive_source_and_quotes_real_extracts(db):
    sm = sc.run(db, 1, NOW, get=web())
    assert sm["docs_read"] == 2 and sm["overall"] == "PASS", [(i["key"], i["final"], i["evidence"]) for i in sm["items"] if i["final"] not in ("PASS", "N/A")]
    page_text = " ".join(sc.open_pages(db, 1, get=web())[0].values())
    for i in sm["items"]:
        if i["final"] == "PASS" and i["evidence"].startswith("…") or (i["key"] in ("figures", "names") and i["final"] == "PASS"):
            core = i["evidence"].strip("…").strip()
            assert core in page_text            # every PASS extract is literally on the page
    assert by(sm, "figures")[0]["final"] == "PASS" and by(sm, "denials")[0]["final"] == "PASS"
    assert by(sm, "corroboration")[0]["claim"].startswith("Independent outlets reporting it: 1")


def test_second_check_fails_figure_not_in_source_and_blocks_approval(db):
    sm = sc.run(db, 1, NOW, get=web(PAGE.replace("$1.2 billion", "a large sum")))
    assert by(sm, "figures")[0]["final"] == "FAIL" and sm["overall"] == "FAIL"
    ed.save_version(db, 1, "article", "text", NOW)
    r = ed.approve(db, 1, NOW)
    assert not r["ok"] and "second check FAIL" in r["error"]


def test_nothing_is_called_pass_when_pages_could_not_be_opened(db):
    sm = sc.run(db, 1, NOW, get=FakeWeb({}))
    assert sm["docs_read"] == 0 and sm["overall"] == "FAIL"
    assert all(i["final"] in ("FAIL", "UNCLEAR", "N/A") for i in sm["items"] if i["material"])


def test_satire_old_story_and_rumour_flags(db):
    sm = sc.run(db, 1, NOW, get=web(PAGE + " This article is satire."))
    assert by(sm, "satire_rumour")[0]["final"] == "FAIL"
    db.execute("UPDATE stories SET published_at='2026-09-01T00:00:00Z'")
    sm = sc.run(db, 1, NOW, get=web(PAGE + " Rumours say otherwise."))
    assert by(sm, "recycled")[0]["final"] == "UNCLEAR"


def test_allegation_stated_as_fact_is_unclear(db):
    page = PAGE.replace("pleaded guilty", "is accused of fraud, allegedly").replace("agreed to pay", "faces")
    db.execute("UPDATE stories SET headline='SEC finds John Smith defrauded investors of $1.2 billion'")
    sm = sc.run(db, 1, NOW, get=web(page))
    assert by(sm, "allegation")[0]["final"] == "UNCLEAR"


def test_human_resolution_unblocks_approval_and_is_logged(db):
    db.execute("UPDATE stories SET headline='Acme Capital chief faces trial over $1.2 billion fraud'")
    sc.run(db, 1, NOW, get=web(PAGE.replace("John Smith", "J. Smith")))
    sm = sc.summary(db, 1)
    assert sm["overall"] != "PASS"
    for i in sm["items"]:
        if i["material"] and i["final"] in ("FAIL", "UNCLEAR"):
            assert sc.resolve(db, 1, i["key"], "PASS", "checked by hand against the docket", NOW)
    assert not sc.resolve(db, 1, "names", "PASS", "", NOW)               # a note is mandatory
    assert sc.summary(db, 1)["overall"] == "PASS" and sc.gate(db, 1)[0]
    assert any(r["kind"] == "second_check_resolved" for r in changelog.for_story(db, 1))


def test_submitted_claude_results_with_invented_evidence_are_downgraded(db):
    res = sc.accept_submitted(db, 1, [
        {"key": "figures", "result": "PASS", "evidence": "The SEC charged Acme Capital Management and its founder John Smith", "claim": "ok"},
        {"key": "quotes", "result": "PASS", "evidence": "This sentence was invented and is not on the page", "claim": "bad"}], NOW, get=web())
    sm = sc.summary(db, 1)
    assert res["downgraded"] == 1
    assert by(sm, "figures")[0]["final"] == "PASS" and by(sm, "quotes")[0]["final"] == "UNCLEAR"
    assert "NOT found" in by(sm, "quotes")[0]["evidence"]


# ---------------------------------------------------------------- change log
def test_changelog_records_chains_and_detects_tampering(db):
    ed.save_version(db, 1, "article", "v1", NOW)
    sc.store(db, 1, [sc._item("headline", "h", "PASS", "x")], NOW)
    ed.approve(db, 1, NOW)
    db.execute("UPDATE claims SET status='DISPUTED'")
    db.commit()
    assert changelog.seal(db) >= 5
    ok, n, bad = changelog.verify(db)
    assert ok and n >= 5
    kinds = {r["kind"] for r in changelog.for_story(db, 1)}
    assert {"version_saved", "approval_approve", "claim_changed", "second_check_run"} <= kinds
    for q in ("DELETE FROM change_log", "UPDATE change_log SET detail='x' WHERE id=1"):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(q)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM approvals")
    # a determined attacker with raw DB access drops the trigger and edits a row: the chain exposes it
    db.execute("DROP TRIGGER cl_no_edit")
    db.execute("UPDATE change_log SET detail='rewritten' WHERE id=2")
    assert changelog.verify(db)[0] is False


def test_saved_versions_are_immutable(db):
    ed.save_version(db, 1, "article", "v1", NOW)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE content_versions SET content='silently changed'")


# ---------------------------------------------------------------- permanent records
def test_stories_with_editorial_history_cannot_be_deleted(db):
    db.execute("INSERT INTO stories(headline,headline_norm,detected_at,last_item_at,score) VALUES('junk','j','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z',5)")
    db.execute("INSERT INTO claims(story_id,claim_text,status) VALUES(2,'c','UNVERIFIED')")
    db.commit()
    db.execute("DELETE FROM claims WHERE story_id=2")
    db.execute("DELETE FROM stories WHERE id=2")                       # unreviewed low-score junk may go
    sc.store(db, 1, [sc._item("headline", "h", "PASS", "x")], NOW)
    ed.save_version(db, 1, "article", "v1", NOW)
    ed.approve(db, 1, NOW)
    for q in ("DELETE FROM stories WHERE id=1", "DELETE FROM claims WHERE story_id=1", "DELETE FROM items WHERE story_id=1"):
        with pytest.raises(sqlite3.DatabaseError, match="permanent record"):
            db.execute(q)
    assert db.execute("SELECT COUNT(*) FROM stories WHERE id=1").fetchone()[0] == 1


# ---------------------------------------------------------------- evidence archive
def test_evidence_archive_keeps_full_copy_only_for_official_records(db, tmp_path):
    cfg = load_all()
    wb = lambda u: ("https://web.archive.org/web/20261124/" + u, "captured now")  # noqa: E731
    rows = evidence.archive_story(db, 1, NOW, str(tmp_path), cfg, get=web(), wb=wb)
    off = next(r for r in rows if r["url"] == URL)
    third = next(r for r in rows if r["url"] == THIRD)
    assert off["official"] == 1 and os.path.exists(tmp_path / off["full_copy_path"]) and off["wayback_url"].startswith("https://web.archive.org/")
    assert third["official"] == 0 and third["full_copy_path"] is None and len(third["extract"]) <= evidence.MAX_EXTRACT
    assert len(off["sha256"]) == 64 and off["http_status"] == 200 and off["retrieved_at"]
    assert not any(f.endswith(".html") for _, _, fs in os.walk(tmp_path / "evidence") for f in fs if third["sha256"][:16] in f)


def test_bundle_and_permanent_bundle_tie_story_id_to_article_url(db, tmp_path):
    cfg = load_all()
    evidence.archive_story(db, 1, NOW, str(tmp_path), cfg, get=web(), wb=lambda u: ("", "no capture"))
    ed.add_correction(db, 1, "Clarified the penalty amount", NOW)
    ed.link_story(db, 1, "article_url", "https://thepostmortem.org/blogs/investigations/acme", NOW)
    rel = evidence.permanent_bundle(db, 1, NOW, str(tmp_path), cfg)
    text = open(tmp_path / rel, encoding="utf-8").read()
    assert "PM-000001" in text and "https://thepostmortem.org/blogs/investigations/acme" in text
    for section in ("## Sources", "## Claims", "## Second check", "## Denials and responses", "## Approvals and versions", "## Corrections", "## Full change log"):
        assert section in text
    assert "Clarified the penalty amount" in text and "INTACT" in text
    idx = json.load(open(tmp_path / "evidence" / "permanent" / "PM-000001" / "index.json"))
    assert idx[-1]["article_url"].endswith("/acme")


# ---------------------------------------------------------------- backups
def test_backup_restores_and_corruption_is_detected(db, tmp_path):
    cfg = load_all()
    data = str(db.execute("PRAGMA database_list").fetchone()[2]).rsplit(os.sep, 1)[0]
    evidence.archive_story(db, 1, NOW, data, cfg, get=web(), wb=lambda u: ("", "x"))
    sc.store(db, 1, [sc._item("headline", "h", "PASS", "x")], NOW)
    db.commit()
    arc = backup.make_backup(data, str(tmp_path / "out"), NOW)
    res = backup.restore_test(arc)
    assert res["ok"], res["checks"]
    assert res["counts"]["stories"] == 1 and res["counts"]["evidence_archive"] == 2
    names = tarfile.open(arc).getnames()
    assert "newsroom.db" in names and "MANIFEST.json" in names and any(n.startswith("evidence/") for n in names)
    # the restored copy is a usable database
    dest = str(tmp_path / "restored")
    backup.restore(arc, dest)
    assert sqlite3.connect(os.path.join(dest, "newsroom.db")).execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    with pytest.raises(SystemExit):
        backup.restore(arc, dest)                                         # never restores over a non-empty folder
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(open(arc, "rb").read()[:-40])                         # truncated archive
    assert backup.restore_test(str(bad))["ok"] is False


def test_retention_keeps_12_weekly_and_every_first_of_month():
    ds = [date(2026, 1, 4) + timedelta(weeks=i) for i in range(40)]
    dead = set(backup.to_delete(ds))
    newest12 = sorted(ds, reverse=True)[:12]
    assert not dead & set(newest12)
    firsts = {m: min(d for d in ds if (d.year, d.month) == m) for m in {(d.year, d.month) for d in ds}}
    assert not dead & set(firsts.values())
    assert dead and all(d in ds for d in dead) and len(ds) - len(dead) < 40


# ---------------------------------------------------------------- regressions found on the first real run
def test_headline_repeating_itself_is_not_evidence(db):
    head = "SEC charges John Smith over $1.2 billion fraud"
    page = f"<html><title>{head} | SEC</title><body>{head}. " + "Unrelated filler words here. " * 30 + "</body></html>"
    sm = sc.run(db, 1, NOW, get=FakeWeb({URL: Resp(page.encode()), THIRD: Resp(page.encode())}))
    assert by(sm, "figures")[0]["final"] == "FAIL"            # the figure appears only inside the headline itself
    assert by(sm, "headline")[0]["final"] in ("FAIL", "UNCLEAR")


def test_title_case_headline_does_not_invent_names_and_scripts_are_ignored(db):
    db.execute("UPDATE stories SET headline='New York Father And Son Plead Guilty In Tax Scheme'")
    page = "<html><script>var s='insertBefore(sz, s)';</script><body>" + "A father and son pleaded guilty in a tax scheme. " * 20 + "</body></html>"
    docs, _ = sc.open_pages(db, 1, get=FakeWeb({URL: Resp(page.encode()), THIRD: Resp(page.encode())}))
    assert "insertBefore" not in " ".join(docs.values())
    sm = sc.run(db, 1, NOW, get=FakeWeb({URL: Resp(page.encode()), THIRD: Resp(page.encode())}))
    assert [i["final"] for i in by(sm, "names")] == ["N/A"]
