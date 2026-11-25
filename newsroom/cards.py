"""One plain-English "story card" used by BOTH the dashboard and the 7:00 AM briefing, so they never disagree.
Everything here is deterministic (no AI, no cost) and built only from stored facts: nothing is invented."""
from __future__ import annotations

import json
import re
from datetime import timedelta

from . import editorial as ed, secondcheck
from .package import make_hooks
from .util import human_age, iso, parse_iso

STATUS_WORDS = {
    "UNVERIFIED": "Not yet verified",
    "COMPANY-REPORTED": "Stated by the source itself",
    "INDEPENDENTLY CORROBORATED": "Reported by 2+ outlets (headline match)",
    "VERIFIED": "Verified",
    "REPORTED ALLEGATION": "Reported allegation",
    "DISPUTED": "Disputed",
}
EDITORIAL_WORDS = {"new": "New", "verifying": "Needs review", "ready": "Ready to approve", "approved": "Approved",
                   "published": "Published", "rejected": "Rejected"}


def _clean(text: str, n: int) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


def summary_of(conn, sid: int, headline: str) -> str:
    """Up to ~2 lines from the feed's own excerpt. If the feed gave none (or just repeated the headline), say so."""
    for r in conn.execute("SELECT summary FROM items WHERE story_id=? AND bot!='trends' ORDER BY id LIMIT 4", (sid,)):
        t = _clean(r["summary"], 220)
        if re.search(r"(get our|sign up|subscribe|newsletter|free app|podcast|cookie|advertisement)", t, re.I):
            continue   # feed boilerplate, not a summary
        if len(t) > 40 and t.lower()[:40] != headline.lower()[:40]:
            return t
    return "The feed gave no summary. Open the source to read it."


def why_it_matters(s: dict, sj: dict, n_outlets: int) -> str:
    b = sj.get("breakdown", {})
    bits = [s["category"] or "General"]
    if s.get("primary_url"):
        bits.append("an official/primary record exists")
    if n_outlets >= 2:
        bits.append(f"{n_outlets} outlets report it")
    if b.get("freshness", 0) >= 18:
        bits.append("very fresh")
    if b.get("public_interest", 0) >= 16:
        bits.append("high public interest")
    if sj.get("sensational"):
        bits.append("headline is sensational: check carefully")
    s = "; ".join(bits)
    return s[0].upper() + s[1:] + "."


def _second(conn, sid: int) -> dict:
    sm = secondcheck.summary(conn, sid)
    return {"overall": sm["overall"], "at": sm["at"], "fail": sm["fail"], "unclear": sm["unclear"]}


def card(conn, s, now) -> dict:
    s = dict(s)
    sj = json.loads(s.get("score_json") or "{}")
    items = [dict(r) for r in conn.execute("SELECT org, summary FROM items WHERE story_id=? AND bot!='trends'", (s["id"],))]
    outlets = {i["org"] for i in items}
    ref = parse_iso(s["published_at"]) if s.get("published_at") else None
    age = f"published {human_age((now - ref).total_seconds())} ago" if ref else f"detected {human_age((now - parse_iso(s['detected_at'])).total_seconds())} ago (no publish time)"
    return {
        "id": s["id"], "uid": ed.uid(s["id"]), "headline": _clean(s["headline"], 160), "summary": summary_of(conn, s["id"], s["headline"]),
        "why": why_it_matters(s, sj, len(outlets)), "age": age, "age_min": int(((now - (ref or parse_iso(s["detected_at"]))).total_seconds()) // 60),
        "score": s["score"], "category": s["category"] or "General", "link": s["primary_url"] or s["original_url"] or "",
        "is_primary": bool(s["primary_url"]), "source": s["source_org"] or "",
        "verification": STATUS_WORDS.get(s["verification_status"], s["verification_status"]), "status": s["editorial_status"],
        "status_word": EDITORIAL_WORDS.get(s["editorial_status"], s["editorial_status"]),
        "hook": make_hooks(s["headline"], items[0]["summary"] if items else "")[0],
        "cmd_open": f"/story {s['id']}", "cmd_approve": f"/approve {s['id']}", "cmd_reject": f"/reject {s['id']}",
        "blocked": conn.execute("SELECT COUNT(*) c FROM claims WHERE story_id=? AND status='UNVERIFIED'", (s["id"],)).fetchone()["c"],
        "alert": s["score"] >= 80 and bool(s["evidence_ok"]),
        "second": _second(conn, s["id"]),
        "cmd_second": f"/secondcheck {s['id']}",
    }


def top_stories(conn, now, n=5, hours=24, max_pub_age_hours=48) -> list[dict]:
    """Top stories of the day. A story must be recent by its own publication time (re-listed old items are excluded);
    if the feed gave no publication time it must have been first detected within `hours`."""
    rows = conn.execute(
        "SELECT * FROM stories WHERE lead_only=0 AND last_item_at>=? AND editorial_status NOT IN ('rejected','published') "
        "AND ((published_at IS NOT NULL AND published_at>=?) OR (published_at IS NULL AND detected_at>=?)) "
        "ORDER BY score DESC, last_item_at DESC LIMIT ?",
        (iso(now - timedelta(hours=hours)), iso(now - timedelta(hours=max_pub_age_hours)), iso(now - timedelta(hours=hours)), n)).fetchall()
    return [card(conn, r, now) for r in rows]


def pipeline_counts(conn) -> dict:
    c = {r["editorial_status"]: r["c"] for r in conn.execute("SELECT editorial_status, COUNT(*) c FROM stories WHERE lead_only=0 AND (editorial_status<>'new' OR score>=45) GROUP BY 1")}
    return {"new": c.get("new", 0), "review": c.get("verifying", 0), "ready": c.get("ready", 0), "approved": c.get("approved", 0)}
