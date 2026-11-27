"""Recording a fact sheet: facts start UNVERIFIED, only become checked when the cited text is really on the opened page; notes are never facts;
human steps are never ticked."""
from conftest import NOW, FakeWeb, Resp
from newsroom import review, storyrecord
from newsroom.config import load_all

PAGE_A = b"<html><body>The liquidators repeatedly requested the books and records from the director and she did not provide any.</body></html>"


def spec(url_b="https://b.example/x"):
    return {"story_id": 5, "sources": [
        {"key": "S1", "kind": "official", "publisher": "Agency", "published": "2026-11-26", "url": "https://a.example/x"},
        {"key": "S2", "kind": "press", "publisher": "Paper", "published": "2026-11-26", "url": url_b, "republished_copy": True}],
        "facts": [{"src": "S1", "text": "They asked repeatedly.", "cite": "liquidators repeatedly requested the books and records"},
                  {"src": "S2", "text": "Something not on the page.", "cite": "the director bought a yacht in Monaco last spring"}],
        "notes": {"not-confirmed": ["court record"], "never-use": ["address"]}}


def test_only_text_found_on_the_opened_page_is_marked_and_notes_are_not_facts(conn):
    conn.execute("INSERT INTO stories(id,headline,headline_norm,source_org,detected_at,last_item_at,category,score,pub_known,evidence_ok,editorial_status) "
                 "VALUES(5,'A story','a story','X','2026-11-24T19:10:00Z','2026-11-24T19:10:00Z','Other',50,1,1,'new')")
    conn.commit()
    web = FakeWeb({"https://a.example/x": Resp(PAGE_A), "https://b.example/x": Resp(b"<html><body>nothing relevant here at all</body></html>")})
    out = storyrecord.record(conn, spec(), NOW, "data_tmp_unused", load_all(), get=web, wb=lambda u: ("", "offline"))
    st = {r["claim_text"]: r["status"] for r in conn.execute("SELECT claim_text,status FROM claims WHERE story_id=5")}
    assert st["They asked repeatedly."] == "COMPANY-REPORTED" and st["Something not on the page."] == "UNVERIFIED"
    assert out["text_found"] == 1 and not out["link_check_ok"]
    assert conn.execute("SELECT COUNT(*) c FROM story_notes WHERE story_id=5 AND kind IN ('not-confirmed','never-use')").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM story_notes WHERE kind='republished-copy'").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM claims WHERE story_id=5").fetchone()["c"] == 2        # notes never became claims
    cl = review.checklist(conn, 5)
    assert cl["flags"]["second"] is False and cl["flags"]["links"] is False and not cl["all_ok"]    # human step never ticked


def test_reset_second_check_makes_it_no_and_is_not_repeated(conn):
    from newsroom import secondcheck
    conn.execute("INSERT INTO stories(id,headline,headline_norm,source_org,detected_at,last_item_at,category,score,pub_known,evidence_ok,editorial_status) "
                 "VALUES(6,'B story','b story','X','2026-11-24T19:10:00Z','2026-11-24T19:10:00Z','Other',50,1,1,'new')")
    conn.commit()
    secondcheck.store(conn, 6, [secondcheck._item("headline", "h", "PASS", "x")], NOW)
    assert secondcheck.gate(conn, 6)[0] is True
    sp = dict(spec(), story_id=6, reset_second_check=True)
    web = FakeWeb({"https://a.example/x": Resp(PAGE_A), "https://b.example/x": Resp(b"<html>x</html>")})
    storyrecord.record(conn, sp, NOW, "data_tmp_unused", load_all(), get=web, wb=lambda u: ("", "offline"))
    assert secondcheck.gate(conn, 6)[0] is False
    n = conn.execute("SELECT COUNT(*) c FROM second_checks WHERE story_id=6").fetchone()["c"]
    storyrecord.record(conn, sp, NOW, "data_tmp_unused", load_all(), get=web, wb=lambda u: ("", "offline"))
    assert conn.execute("SELECT COUNT(*) c FROM second_checks WHERE story_id=6").fetchone()["c"] == n
