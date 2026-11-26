import json

import pytest

from conftest import Resp
from newsroom import fetch
from newsroom.fetch import FetchError, fetch_items, parse_html_list

ASIC = '<ul><li><a href="/about-asic/news-centre/news-items/final-call-for-firms">Final call for firms to act before the deadline</a> 4 September 2026</li>' \
       '<li><a href="/newsroom/media-releases/">Media releases</a></li></ul>'
TREAS = '<div><time datetime="2026-11-23T18:00:00Z">November 23, 2026</time><h3><a href="/news/press-releases/sb0699/">Operation Outcast disrupts shadow banking</a></h3></div>' \
        '<div><time datetime="2026-11-20T21:00:00Z">November 20, 2026</time><h3><a href="/news/press-releases/sb0698/">Treasury identifies potential shadow banking</a></h3></div>'
AFP = '<div class="card"><a href="/news-centre/media-release/man-charged-over-threat" aria-label="Man charged over threat to parliamentarian">View</a></div>'
ICJ = '<a href="/sites/default/files/press-releases/0/000-20261109-pre-01-00-en.pdf">Press release No. 2026/29</a>'


def src(**kw):
    return dict({"id": "x", "org": "Org", "bot": "primary", "tier": "primary", "url": "https://example.gov/news"}, **kw)


def test_press_page_items_are_tagged_official_and_dated():
    it = parse_html_list(ASIC.encode(), src(html_list={"href_regex": "/news-items/"}))
    assert len(it) == 1 and it[0]["url"] == "https://example.gov/about-asic/news-centre/news-items/final-call-for-firms"
    assert it[0]["published_at"].strftime("%Y-%m-%d") == "2026-09-04" and "official-source" in it[0]["tags"] and "press-page" in it[0]["tags"]


def test_date_before_and_headline_in_aria_label_and_date_in_url():
    t = parse_html_list(TREAS.encode(), src(html_list={"href_regex": r"/press-releases/sb\d+/?$", "date_from": "before"}))
    assert [x["published_at"].strftime("%Y-%m-%d") for x in t] == ["2026-11-23", "2026-11-20"]      # each headline gets ITS OWN date
    a = parse_html_list(AFP.encode(), src(html_list={"href_regex": r"/media-release/"}))
    assert a[0]["title"] == "Man charged over threat to parliamentarian"
    j = parse_html_list(ICJ.encode(), src(html_list={"href_regex": r"-en\.pdf$", "date_from": "url", "min_title": 10}))
    assert j[0]["published_at"].strftime("%Y-%m-%d") == "2026-11-09"


def test_robots_txt_is_respected_and_google_news_is_labelled():
    fetch._ROBOTS.clear()
    calls = []

    def get(url, **kw):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return Resp(b"User-agent: *\nDisallow: /news\n")
        return Resp(ASIC.encode())
    with pytest.raises(FetchError) as ei:
        fetch_items(src(html_list={"href_regex": "/news-items/"}), get)
    assert ei.value.status == 403 and calls == ["https://example.gov/robots.txt"]       # the page itself was never requested
    assert "via-google-news" in fetch._tags(src(google_news=True)) and "official-source" in fetch._tags(src(google_news=True))


def test_all_six_requested_sources_are_configured_with_a_method_or_a_reason():
    cfg = json.load(open("config/sources.json", encoding="utf-8"))["sources"]
    by = {s["id"]: s for s in cfg}
    for sid in ("asic-media", "afp-au", "fbi-press", "treasury-press", "ofac-actions", "icj-press"):
        s = by[sid]
        assert s.get("official") and (s.get("enabled", True) or s.get("disabled_reason")), sid
        gn = [x for x in cfg if x.get("google_news") and x["id"].startswith(sid.split("-")[0])]
        assert gn and "via Google News" in gn[0]["org"], sid                                  # method (c) is always prepared


def test_doj_sample_card_shows_summary_sources_checklist_and_can_never_be_published(conn, tg, tmp_path):
    from conftest import NOW
    from newsroom import editorial as ed, review, siteexport
    n, t = tg
    assert review.send_sample_card(conn, n, NOW) == "sent"
    msg = t.sent[-1]
    assert "SAMPLE" in msg and "never be published" in msg and "Checklist" in msg and "Summary" in msg and "Sources" in msg and "doj.gov/p1" in msg
    assert [b["callback_data"].split(":")[1] for row in t.markups[-1]["inline_keyboard"] for b in row][:2] == ["approve", "more"]
    sid = review.ensure_sample_card(conn, NOW)
    out = review.apply_action(conn, "approve", sid, NOW, data_dir=str(tmp_path))
    assert "never be approved" in out["text"] and conn.execute("SELECT COUNT(*) FROM approvals WHERE story_id=?", (sid,)).fetchone()[0] == 0
    assert ed.approve(conn, sid, NOW)["ok"] is False
    assert siteexport.build(conn, NOW)["stories"] == []


def test_watcher_heartbeat_starts_overdue_workflows_only():
    from datetime import timedelta
    from conftest import NOW
    from newsroom import tapwatch
    iso = lambda m: (NOW - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = {"cycle.yml": [{"status": "completed", "created_at": iso(45)}], "health.yml": [{"status": "completed", "created_at": iso(10)}]}
    started = []
    assert tapwatch.heartbeat(NOW, lambda w: runs[w], started.append) == ["cycle.yml"] and started == ["cycle.yml"]
    runs["cycle.yml"] = [{"status": "in_progress", "created_at": iso(90)}]          # already running: leave it alone
    assert tapwatch.heartbeat(NOW, lambda w: runs[w], started.append) == []


def test_cycle_rescoring_skips_test_cards_and_itemless_stories(conn, cfg):
    from conftest import NOW
    from newsroom import ingest, review
    tid = review.ensure_test_card(conn, NOW)        # a test card has no items at all
    sid = review.ensure_sample_card(conn, NOW)
    ingest.rescore_recent(conn, NOW, cfg)           # must not raise
    row = conn.execute("SELECT category, lead_only FROM stories WHERE id IN (?,?) ORDER BY id", (tid, sid)).fetchall()
    assert [r["category"] for r in row] == ["TEST CARD", "SAMPLE"] and all(r["lead_only"] == 1 for r in row)
