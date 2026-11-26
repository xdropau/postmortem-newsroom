"""BOT 05 - verification ledger.

Honest scope: automation can (a) confirm a source URL really opens, (b) confirm that figures and quotations from the
headline/summary literally appear in an opened source, and (c) count independent outlets. Everything else (names,
dates, legal status, denials, context) is created as a MANUAL claim that stays UNVERIFIED until a human resolves it.
A story cannot be approved while any claim is UNVERIFIED.
"""
from __future__ import annotations

import re

from .fetch import FetchError, http_get
from .score import is_primary_domain
from .util import iso, sha256_bytes, strip_html

STATUSES = ["VERIFIED", "COMPANY-REPORTED", "INDEPENDENTLY CORROBORATED", "REPORTED ALLEGATION", "DISPUTED", "UNVERIFIED"]
_ROLLUP = ["DISPUTED", "UNVERIFIED", "REPORTED ALLEGATION", "COMPANY-REPORTED", "INDEPENDENTLY CORROBORATED", "VERIFIED"]

_FIG = re.compile(r"(?:(?:US|A|AU)?\$|£|€)?\s?\d[\d,]*(?:\.\d+)?\s?(?:billion|million|trillion|bn|mn|m|%|percent)?", re.I)
_QUOTE = re.compile(r"[\"“‘']([^\"”’']{18,200})[\"”’']")
_UNIT = {"billion": r"(?:billion|bn|b)", "bn": r"(?:billion|bn|b)", "million": r"(?:million|mn|m)", "mn": r"(?:million|mn|m)",
         "m": r"(?:million|mn|m)", "trillion": r"(?:trillion|tn|t)", "%": r"(?:%|percent|per cent)", "percent": r"(?:%|percent|per cent)"}


def extract_figures(text: str) -> list[str]:
    out = []
    for m in _FIG.finditer(text or ""):
        s = m.group(0).strip()
        digits = re.sub(r"\D", "", s)
        # skip bare small numbers / years: only keep figures with a currency symbol, unit, or 4+ digits that are not a year
        has_unit = bool(re.search(r"[$£€%]|billion|million|trillion|bn|mn|percent|\dm\b", s, re.I))
        if has_unit or (len(digits) >= 4 and not re.fullmatch(r"(19|20)\d\d", digits)):
            out.append(s)
    return list(dict.fromkeys(out))[:6]


def figure_in_text(fig: str, doc: str) -> bool:
    m = re.match(r"(?:(?:US|A|AU)?\$|£|€)?\s?(\d[\d,]*(?:\.\d+)?)\s?([a-z%]*)", fig.strip(), re.I)
    if not m:
        return False
    num, unit = m.group(1), m.group(2).lower()
    num_pat = re.escape(num).replace(r"\,", ",?")
    pat = r"(?<![\d.])" + num_pat + (r"\s?" + _UNIT[unit] if unit in _UNIT else r"(?![\d])")
    return re.search(pat, doc or "", re.I) is not None


def extract_quotes(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1).strip() for m in _QUOTE.finditer(text or "")))[:3]


def story_status(conn, story_id: int) -> str:
    sts = {r["status"] for r in conn.execute("SELECT status FROM claims WHERE story_id=?", (story_id,))}
    if not sts:
        return "UNVERIFIED"
    for s in _ROLLUP:
        if s in sts:
            return s
    return "UNVERIFIED"


def refresh_status(conn, story_id: int) -> str:
    st = story_status(conn, story_id)
    conn.execute("UPDATE stories SET verification_status=? WHERE id=?", (st, story_id))
    conn.commit()
    return st


def outstanding(conn, story_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, claim_text, status FROM claims WHERE story_id=? AND status IN ('UNVERIFIED','DISPUTED') ORDER BY id", (story_id,))]


def can_approve(conn, story_id: int) -> tuple[bool, list[dict]]:
    n = conn.execute("SELECT COUNT(*) c FROM claims WHERE story_id=?", (story_id,)).fetchone()["c"]
    out = [o for o in outstanding(conn, story_id) if o["status"] == "UNVERIFIED"]
    return (n > 0 and not out), out


def seed_claims(conn, story_id: int, cfg, now):
    if conn.execute("SELECT 1 FROM claims WHERE story_id=?", (story_id,)).fetchone():
        return
    s = dict(conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone())
    items = [dict(r) for r in conn.execute("SELECT * FROM items WHERE story_id=?", (story_id,))]
    orgs = {i["org"] for i in items if i["bot"] != "trends"}
    text = " ".join([s["headline"]] + [i["summary"] or "" for i in items[:4]])

    def add(ctype, claim, status="UNVERIFIED", manual=0, url=None, evidence=None):
        url = url or s["primary_url"] or s["original_url"]     # every claim must link to a source record
        conn.execute("INSERT INTO claims(story_id, claim_type, claim_text, status, manual, source_url, evidence, checked_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (story_id, ctype, claim, status, manual, url, evidence, iso(now)))

    add("source", "A source document or page exists at the primary/original URL and could be opened by this system",
        url=s["primary_url"] or s["original_url"])
    if s["primary_url"]:
        add("authenticity", "The primary document is served from an official domain",
            "VERIFIED" if is_primary_domain(s["primary_url"], cfg) else "UNVERIFIED", url=s["primary_url"],
            evidence="official-domain list match" if is_primary_domain(s["primary_url"], cfg) else None)
        add("core", "The core assertion is stated by the issuing body itself (self-reported, not independently confirmed)",
            "COMPANY-REPORTED", url=s["primary_url"])
    else:
        add("core", f"The core assertion is independently reported by at least two outlets ({', '.join(sorted(orgs))})",
            "INDEPENDENTLY CORROBORATED" if len(orgs) >= 2 else "UNVERIFIED",
            evidence="headline-level match only; wording may derive from one wire" if len(orgs) >= 2 else None)
    for fig in extract_figures(text):
        add("figure", f"Figure \"{fig}\" is stated in the source", url=s["primary_url"] or s["original_url"])
    for q in extract_quotes(text):
        add("quote", f"Quotation \"{q}\" appears verbatim in the source and is correctly attributed", url=s["original_url"])
    for ctype, claim in (("names", "Names and identities of all people and organisations are correct"),
                         ("dates", "Dates and timeline are correct"),
                         ("money", "All financial figures are correct and in context (currency, period)"),
                         ("legal", "Legal proceedings and corporate relationships are accurately described"),
                         ("allegations", "Allegations are clearly attributed, and denials or responses are included")):
        add(ctype, claim, manual=1)
    conn.commit()


def open_sources(conn, story_id: int, now, get=http_get, max_docs: int = 3) -> dict:
    """Actually fetch sources and test claims against them. Records every attempt; never claims an unopened source was opened."""
    s = dict(conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone())
    items = [dict(r) for r in conn.execute("SELECT * FROM items WHERE story_id=? ORDER BY id", (story_id,))]
    urls = []
    for u in [s["primary_url"]] + [i["url"] for i in items]:
        if u and u not in urls:
            urls.append(u)
    docs: dict[str, str] = {}
    for u in urls[:max_docs]:
        try:
            r = get(u, timeout=20, retries=2, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.5"})
            text = strip_html(r.text, limit=400_000)
            docs[u] = text
            conn.execute("INSERT INTO source_checks(story_id,url,opened_at,http_status,bytes,sha256) VALUES(?,?,?,?,?,?)",
                         (story_id, u, iso(now), r.status_code, len(r.content), sha256_bytes(r.content)))
        except (FetchError, Exception) as e:  # noqa: BLE001 - any failure is recorded, never hidden
            conn.execute("INSERT INTO source_checks(story_id,url,opened_at,http_status,error) VALUES(?,?,?,?,?)",
                         (story_id, u, iso(now), getattr(e, "status", None), str(e)[:200]))
    conn.commit()
    target = s["primary_url"] or s["original_url"]
    if target in docs and len(docs[target]) > 200:
        conn.execute("UPDATE claims SET status='VERIFIED', evidence=?, checked_at=? WHERE story_id=? AND claim_type='source'",
                     (f"opened {target} (HTTP 200, {len(docs[target])} chars)", iso(now), story_id))
    for c in conn.execute("SELECT id, claim_type, claim_text FROM claims WHERE story_id=? AND claim_type IN ('figure','quote') "
                          "AND status='UNVERIFIED'", (story_id,)).fetchall():
        m = re.search(r'"(.+)"', c["claim_text"])
        needle = m.group(1) if m else ""
        found_in = None
        for u, d in docs.items():
            if c["claim_type"] == "figure" and figure_in_text(needle, d):
                found_in = u
            elif c["claim_type"] == "quote" and needle.lower() in d.lower():
                found_in = u
            if found_in:
                break
        if found_in:
            conn.execute("UPDATE claims SET status='VERIFIED', evidence=?, source_url=?, checked_at=? WHERE id=?",
                         (f"found verbatim in opened source {found_in}", found_in, iso(now), c["id"]))
    conn.commit()
    refresh_status(conn, story_id)
    return {"opened": list(docs), "attempted": urls[:max_docs]}


def set_claim(conn, story_id: int, claim_id: int, status: str, note: str, now) -> bool:
    status = status.upper().replace("_", " ")
    if status not in STATUSES:
        return False
    cur = conn.execute("UPDATE claims SET status=?, note=?, checked_at=? WHERE id=? AND story_id=?",
                       (status, note[:300], iso(now), claim_id, story_id))
    conn.commit()
    refresh_status(conn, story_id)
    return cur.rowcount > 0
