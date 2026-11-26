"""BOT 04 - deterministic story scoring (100 points). No AI involved.

Freshness 25 | Public interest 20 | Evidence availability 25 | Audience relevance 15 |
Original investigative angle 10 | Visual storytelling 5.
A priority alert needs score >= threshold AND the evidence gate AND no unsupported sensationalism.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .util import domain_of, iso, parse_iso

_MONEY = re.compile(r"(\$|A\$|US\$|£|€)\s?\d|\b\d[\d,.]*\s?(billion|million|trillion|bn|%)", re.I)
_cache: dict = {}


def _compiled(cfg):
    key = id(cfg["keywords"])
    if key not in _cache:
        k = cfg["keywords"]

        def comp(words):
            return [re.compile(w if "\\" in w else r"\b" + re.escape(w), re.I) for w in words]
        _cache[key] = {
            "cats": {c: comp(ws) for c, ws in k["categories"].items()},
            "aus": comp(k["australia"]), "aud": comp(k["audience_topics"]),
            "inv": comp(k["investigative_terms"]), "sens": comp(k["sensational_terms"]),
        }
    return _cache[key]


def _hits(patterns, text):
    return sum(1 for p in patterns if p.search(text))


def freshness_points(age_minutes: float, pub_known: bool) -> int:
    for limit, pts in ((30, 25), (60, 22), (180, 18), (360, 12), (1440, 6)):
        if age_minutes <= limit:
            return min(pts, 12) if not pub_known else pts
    return 0


_LIVE = re.compile(r"(/live/|\blive\s*$|\blive blog\b|\blive updates?\b|\blive:|[-–—:]\s*live\b)", re.I)


def is_live_blog(headline: str, urls) -> bool:
    return bool(_LIVE.search(headline or "") or any("/live/" in (u or "") or "/live-" in (u or "") for u in urls))


def classify_sec_8k(story: dict, items: list[dict], cfg: dict) -> dict | None:
    """Deterministic triage of EDGAR 8-K filings. Returns None for non-8-K stories, else {kind, cap, reasons}.
    Routine filings (SPACs, shells, small filers, no event signal) get a low cap so they never fill the top 5. A filing
    ranks normally only when its content signals a real event, a large company is involved, or independent outlets
    also cover it. Pure keyword/regex rules, no AI."""
    k = cfg["keywords"].get("sec_8k")
    if not k:
        return None
    sec = [i for i in items if i["source_id"] in k["source_ids"]]
    if not sec:
        return None
    name = " ".join([story["headline"]] + [i["title"] or "" for i in sec]).lower()
    text = " ".join([name] + [i["summary"] or "" for i in sec]).lower()
    codes = set(re.findall(r"item\s+(\d\.\d{2})", text))
    shell = any(re.search(p, name) for p in k["shell_name_patterns"])
    large = any(w in name for w in k["large_filers"])
    outlets = {i["org"] for i in items if i["bot"] != "trends" and i["source_id"] not in k["source_ids"]}
    covered = len(outlets) >= k["coverage_min_outlets"]
    terms = [t for t in k["real_event_terms"] if t in text]
    hard = sorted(codes & set(k["real_event_item_codes"]))
    cond = sorted(codes & set(k["conditional_item_codes"]))
    if hard or (terms and not shell) or covered:
        return {"kind": "real-event", "cap": 100, "reasons": hard + terms[:3] + (["news coverage"] if covered else [])}
    if cond and large:
        return {"kind": "large-company-event", "cap": 100, "reasons": cond + ["large company"]}
    if shell:
        return {"kind": "routine-shell", "cap": k["routine_cap"], "reasons": ["SPAC/shell filer, no real-event signal"]}
    if cond:
        return {"kind": "unsupported-event", "cap": k["conditional_unsupported_cap"], "reasons": cond + ["small filer, no coverage"]}
    return {"kind": "routine", "cap": k["routine_cap"], "reasons": ["no real-event signal"]}


def is_primary_domain(url: str, cfg) -> bool:
    d = domain_of(url)
    return any(d == p or d.endswith("." + p) for p in cfg["settings"]["primary_domains"])


def score_story(story: dict, items: list[dict], now: datetime, trend_terms: set[str], cfg: dict) -> dict:
    c = _compiled(cfg)
    news_items = [i for i in items if i["bot"] != "trends"]
    lead_only = 1 if items and not news_items else 0
    text = " ".join([story["headline"]] + [i["summary"] or "" for i in items[:6]]).lower()
    headline = story["headline"].lower()

    known = [parse_iso(i["published_at"]) for i in items if i["pub_known"] and i["published_at"]]
    ref = min(known) if known else parse_iso(story["detected_at"])
    age_min = max(0.0, (now - ref).total_seconds() / 60)
    fresh = freshness_points(age_min, bool(known))

    cats = [cat for cat in cfg["keywords"]["priority_order"] if _hits(c["cats"][cat], text) > 0]
    if is_live_blog(story["headline"], [i["url"] for i in items]) and _hits(c["cats"]["Politics"], headline) > 0:
        cats = ["Politics"] + [x for x in cats if x != "Politics"]   # a politics live blog is politics, whatever else the summary mentions
    orgs = sorted({i["org"] for i in news_items})
    primary_items = [i for i in news_items if i["tier"] == "primary"]
    primary_link = None
    for i in news_items:
        for l in json.loads(i["primary_links"] or "[]"):
            primary_link = primary_link or l
    primary_url = primary_items[0]["url"] if primary_items else primary_link

    interest = 0
    if cats:
        interest += 8 + min(6, 3 * (len(cats) - 1))
    interest += 6 if len(orgs) >= 4 else 3 if len(orgs) >= 2 else 0
    trend_hit = any(t in headline for t in trend_terms)
    interest += 4 if trend_hit else 0
    interest = min(20, interest)

    evid = 0
    if primary_items:
        evid = 22 + (3 if len(orgs) >= 2 else 0)
    else:
        evid = 0 if not news_items else 6 if len(orgs) == 1 else 12 if len(orgs) == 2 else 16
        if primary_link:
            evid += 8
    evid = min(25, evid)
    evidence_ok = bool(primary_items) or len(orgs) >= 2 or bool(primary_link)

    aud = 0
    if _hits(c["aus"], text):
        aud += 8
    if _hits(c["aud"], text):
        aud += 4
    if any(cat in ("Financial crime", "Consumer exploitation") for cat in cats):
        aud += 3
    aud = min(15, aud)

    angle = min(10, 2 * _hits(c["inv"], text) + (3 if primary_items else 0))
    visual = (3 if _MONEY.search(story["headline"]) else 0) + (2 if any(i["has_image"] for i in items) else 0)

    sensational = _hits(c["sens"], story["headline"].lower()) > 0
    total = fresh + interest + evid + aud + angle + visual
    if sensational:
        total = max(0, total - 8)
    sec8k = classify_sec_8k(story, items, cfg)
    if sec8k:
        total = min(total, sec8k["cap"])
    if lead_only:
        total = min(total, 60)  # social/search trends are discovery leads, never alert-grade on their own

    return {
        "score": int(total), "category": cats[0] if cats else "Uncategorised", "categories": cats,
        "breakdown": {"freshness": fresh, "public_interest": interest, "evidence": evid, "audience": aud,
                      "investigative_angle": angle, "visual": visual, "sensational_penalty": 8 if sensational else 0},
        "sec_8k": sec8k,
        "age_minutes": round(age_min, 1), "pub_known": bool(known), "orgs": orgs,
        "evidence_ok": evidence_ok, "sensational": sensational, "lead_only": lead_only,
        "primary_url": primary_url, "trend_hit": trend_hit,
        "published_at": iso(ref) if known else None,
    }


def rescore_story(conn, story_id: int, now: datetime, trend_terms: set[str], cfg: dict) -> dict:
    story = dict(conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone())
    items = [dict(r) for r in conn.execute("SELECT * FROM items WHERE story_id=? ORDER BY id", (story_id,))]
    r = score_story(story, items, now, trend_terms, cfg)
    first_pub = r["published_at"]
    orig = next((i for i in items if i["pub_known"]), items[0])
    primary = next((i for i in items if i["tier"] == "primary"), None)
    conn.execute(
        """UPDATE stories SET score=?, score_json=?, category=?, evidence_ok=?, sensational=?, lead_only=?,
           primary_url=COALESCE(?, primary_url), published_at=?, pub_known=?, source_org=?, original_url=? WHERE id=?""",
        (r["score"], json.dumps(r), r["category"], int(r["evidence_ok"]), int(r["sensational"]), r["lead_only"],
         r["primary_url"], first_pub, int(r["pub_known"]),
         (primary or orig)["org"], (primary or orig)["url"], story_id))
    conn.commit()
    return r
