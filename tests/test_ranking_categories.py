"""8-K triage (routine SPAC/shell filings must not fill the top 5), live-blog categories, headlines-only audit."""
import json
from datetime import timedelta

from conftest import NOW
from newsroom.cards import top_stories
from newsroom.fetch import FetchError
from newsroom.ingest import ingest, rescore_recent
from newsroom.tools import audit
from test_pipeline import item, Resp


def f8k(company, items, summary_extra=""):
    return dict(item(f"8-K - {company} (0001234567) (Filer)", f"https://www.sec.gov/{abs(hash(company))}", org="US SEC EDGAR",
                     summary=f"Filed: 2026-11-25 Items: {items} {summary_extra}"), source_id="sec-8k-material")


def score_of(conn, cfg, it):
    ingest(conn, [it], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    row = conn.execute("SELECT score, score_json FROM stories ORDER BY id DESC LIMIT 1").fetchone()
    return row["score"], json.loads(row["score_json"])["sec_8k"]


def test_spac_routine_8k_scores_low(conn, cfg):
    s, c = score_of(conn, cfg, f8k("Blue Ocean Acquisition Corp", "Item 5.02 Departure of directors; Item 3.01 Notice of delisting"))
    assert s <= 30 and c["kind"] == "routine-shell"


def test_item_headings_alone_are_not_event_content(conn, cfg):
    # "Notice of Delisting" is only the standard heading of Item 3.01; for a SPAC it is routine boilerplate.
    s, c = score_of(conn, cfg, f8k("Omega Merger Corp. II", "Item 3.01: Notice of Delisting or Failure to Satisfy a Continued Listing Rule"))
    assert c["kind"] == "routine-shell" and s <= 30
    s, c = score_of(conn, cfg, f8k("Real Operating Co", "Item 3.01: Notice of Delisting or Failure to Satisfy a Continued Listing Rule"))
    assert c["kind"] == "operating-event" and s <= 50
    assert conn.execute("SELECT category FROM stories ORDER BY id DESC LIMIT 1").fetchone()["category"] == "SEC 8-K filing"


def test_small_filer_executive_departure_is_capped(conn, cfg):
    s, c = score_of(conn, cfg, f8k("Tiny Widgets Inc", "Item 5.02 Departure of Directors or Certain Officers"))
    assert s <= 40 and c["kind"] == "unsupported-event"


def test_large_company_executive_departure_ranks(conn, cfg):
    s, c = score_of(conn, cfg, f8k("Boeing Co", "Item 5.02 Departure of Directors or Certain Officers"))
    assert c["kind"] == "large-company-event" and s > 40


def test_real_event_items_rank_even_for_small_filers_and_spacs(conn, cfg):
    s1, c1 = score_of(conn, cfg, f8k("Acme Small Corp", "Item 4.02 Non-Reliance on Previously Issued Financial Statements"))
    s2, c2 = score_of(conn, cfg, f8k("Zeta Acquisition Corp", "Item 1.05 Material Cybersecurity Incidents"))
    assert c1["kind"] == c2["kind"] == "real-event" and s1 > 40 and s2 > 40


def test_small_filer_with_event_wording_ranks(conn, cfg):
    s, c = score_of(conn, cfg, f8k("Mid Co", "Item 8.01 Other Events", "Filed voluntary petition under Chapter 11 bankruptcy"))
    assert c["kind"] == "real-event"


def test_top5_not_filled_by_routine_8ks(conn, cfg):
    for i in range(8):
        ingest(conn, [f8k(f"Foo{i} Acquisition Corp", "Item 5.02 Departure of directors")], NOW, cfg)
    ingest(conn, [item("Regulator fines bank $20 million over money laundering failures in Australia", "https://asic.gov.au/x", org="ASIC")], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    top = top_stories(conn, NOW)
    assert "money laundering" in top[0]["headline"].lower()
    assert top[0]["score"] > 30 >= max(c["score"] for c in top[1:])


def test_non_sec_scoring_unaffected(conn, cfg):
    _, c = score_of(conn, cfg, item("Bank fined $5 million", "https://x.com/a", "ASIC"))
    assert c is None


def test_politics_live_blog_is_politics_not_tech(conn, cfg):
    it = item("Australian politics live: Albanese and Coalition clash in parliament as minister defends AI regulation",
              "https://www.theguardian.com/australia-news/live/2026/nov/26/politics-live", "The Guardian", "radar", "outlet",
              summary="Artificial intelligence and OpenAI also come up in question time.")
    ingest(conn, [it], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    assert conn.execute("SELECT category FROM stories").fetchone()["category"] == "Politics"


def test_non_live_ai_story_stays_technology(conn, cfg):
    ingest(conn, [item("OpenAI launches new chatbot model", "https://www.theguardian.com/technology/2026/nov/26/openai", "The Guardian", "radar", "outlet")], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    assert conn.execute("SELECT category FROM stories").fetchone()["category"] == "Technology and AI"


def test_audit_headlines_only_403_is_ok_but_other_403_fails(conn, cfg, tg):
    n, _ = tg
    feed = (b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
            b"<item><title>Headline</title><link>https://paywalled.example/a</link></item></channel></rss>")

    def get(url, retries=None):
        if url.endswith("feed"):
            return Resp(feed)
        raise FetchError("HTTP 403", 403)
    base = {"org": "X", "bot": "radar", "tier": "outlet"}
    cfg2 = {**cfg, "sources": {"sources": [dict(base, id="ho", url="https://ho/feed", headlines_only=True),
                                          dict(base, id="plain", url="https://plain/feed")]}}
    rows = {r["id"]: r for r in audit(conn, cfg2, n, NOW, get=get)}
    assert rows["ho"]["status"] == "OK" and rows["ho"].get("note")
    assert rows["plain"]["status"] == "LINK 403"


def test_configured_feeds_and_disabled_reasons(cfg):
    for s in cfg["sources"]["sources"]:
        if s.get("enabled", True) is False:
            assert s.get("disabled_reason"), s["id"]
    ids = {s["id"]: s for s in cfg["sources"]["sources"]}
    assert ids["accc-media"]["enabled"] is False
    for k in ("ft-home", "bloomberg-markets", "scmp-news", "uk-parliament-news", "aph-news"):
        assert ids[k].get("headlines_only") and ids[k]["headlines_only_reason"]
