"""SECOND CHECK: an independent re-read of a story's stored sources before approval.

Rules (hard):
* Every source page is re-fetched NOW and read. Nothing is called PASS unless the text was opened and the evidence
  extract is quoted from that text. Extracts are always literal substrings of the page.
* Result per check: PASS / FAIL / UNCLEAR (N/A when the story contains nothing to test).
* MATERIAL checks block approval while FAIL or UNCLEAR (unless a human resolves them, recorded with a note and time).
  ADVISORY checks are shown but never block.
* What automation cannot do (confirm a person's real identity, legal accuracy, whether an outlet reworded a wire) stays
  UNCLEAR or advisory; it is never guessed.
"""
from __future__ import annotations

import json
import os
import re
from datetime import timedelta

from .fetch import http_get
from .util import iso, parse_iso, strip_html
from .verify import extract_figures, extract_quotes, figure_in_text

MATERIAL = ("source_opened", "headline", "figures", "names", "quotes", "recycled", "satire_rumour", "allegation")
ADVISORY = ("dates", "denials", "contradiction", "corroboration")
LABELS = {"source_opened": "Source pages opened and read", "headline": "Headline supported by the text", "figures": "Figures",
          "names": "Names appear in the source", "quotes": "Quotations are verbatim", "recycled": "New, not a recycled old story",
          "satire_rumour": "Not satire or rumour", "allegation": "Allegation vs established fact", "dates": "Dates",
          "denials": "Denial or response present", "contradiction": "Contradicting material", "corroboration": "Independent corroboration"}
STOP = set("this that with from have been were will their about after into over than then them they what when where which while would could "
           "says said new its are and the for has was but not you your more most also just says say who how why can may out one two".split())
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
SATIRE = re.compile(r"\b(satire|satirical|parody|the onion|babylon bee|not a real news)\b", re.I)
RUMOUR = re.compile(r"\b(rumou?rs?|unconfirmed|unverified (?:claims|reports)|social media (?:posts|users) (?:claim|claimed|say)|viral (?:post|video|claim))\b", re.I)
ALLEG = re.compile(r"\b(alleged(?:ly)?|accused|charged|indicted|indictment|suspect(?:ed)?|prosecutors say|reportedly|claims?|claimed)\b", re.I)
ESTAB = re.compile(r"\b(pleaded guilty|pled guilty|convicted|found guilty|sentenced|ruled|ordered to pay|fined|agreed to pay|settled|admitted)\b", re.I)
HEDGE = re.compile(r"\b(alleged|allegedly|accused|charged|indicted|reportedly|claims?|says?|said|suspected|plead|pleaded|convicted|sentenced|:)", re.I)
DENIAL = re.compile(r"\b(denied|denies|deny|denial|disputes?|disputed|rejected the|did not respond|declined to comment|no comment|in a statement|responded)\b", re.I)
CONTRA = re.compile(r"\b(contradict\w*|conflicting|inconsistent with|disputed|disputes)\b", re.I)
NAME = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+(?:[A-Z]\.|[A-Z][a-z]{2,})){1,2})\b")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


_STATE = {"head": ""}


def _mask(text: str, head: str) -> str:
    """Same-length copy of the page with every occurrence of the story's own headline blanked, so that evidence has to come
    from somewhere other than the headline repeating itself (page titles, breadcrumbs)."""
    if not head or len(head) < 12:
        return text
    return re.sub(re.escape(head), lambda m: " " * len(m.group(0)), text, flags=re.I)


def decode_text(r) -> str:
    """UTF-8 first (many government pages omit the charset and requests would guess Latin-1), else the library's guess."""
    try:
        return r.content.decode("utf-8")
    except Exception:  # noqa: BLE001
        return r.text


def clean_html(raw: str) -> str:
    """Drop scripts, styles and comments before text extraction so code never appears as 'evidence'."""
    raw = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", raw or "", flags=re.S | re.I)
    return re.sub(r"<!--.*?-->", " ", raw, flags=re.S)


def _extract(text: str, needle_re, width: int = 110) -> str:
    """A literal window of the page around the first match OUTSIDE the headline text (always a real substring)."""
    src = _mask(text, _STATE["head"])
    m = needle_re.search(src) if hasattr(needle_re, "search") else re.search(re.escape(needle_re), src, re.I)
    if not m:
        return ""
    a = max(0, m.start() - width // 2)
    b = min(len(text), m.end() + width // 2)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def _item(key, claim, result, evidence="", url="") -> dict:
    return {"key": key, "label": LABELS[key], "claim": claim[:240], "result": result, "evidence": evidence[:300], "source_url": url,
            "material": key in MATERIAL}


def open_pages(conn, sid: int, get=http_get, max_docs: int = 4) -> tuple[dict, list[tuple[str, str]]]:
    s = conn.execute("SELECT primary_url, original_url FROM stories WHERE id=?", (sid,)).fetchone()
    urls = []
    for u in [s["primary_url"], s["original_url"]] + [r["url"] for r in conn.execute("SELECT url FROM items WHERE story_id=? ORDER BY id", (sid,))]:
        if u and u not in urls:
            urls.append(u)
    docs, failed = {}, []
    for u in urls[:max_docs]:
        try:
            r = get(u, timeout=20, retries=2, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.5"})
            t = _norm(strip_html(clean_html(decode_text(r)), limit=400_000))
            if len(t) >= 200:
                docs[u] = t
            else:
                failed.append((u, "page text too short to read"))
        except Exception as ex:  # noqa: BLE001
            failed.append((u, f"{getattr(ex, 'status', None) or type(ex).__name__}"))
    return docs, failed


def run_checks(conn, sid: int, now, docs: dict, failed: list) -> list[dict]:
    s = dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
    items = [dict(r) for r in conn.execute("SELECT * FROM items WHERE story_id=? ORDER BY id", (sid,))]
    head = s["headline"]
    summ = " ".join((i["summary"] or "") for i in items[:3])
    alltext = " ".join(docs.values())
    _STATE["head"] = head
    masked = _mask(alltext, head)
    first_url = next(iter(docs), "")
    out = []

    # 1. sources opened
    if not docs:
        out.append(_item("source_opened", "Stored source pages can be re-opened", "FAIL",
                         "No source page could be opened and read: " + "; ".join(f"{u} ({e})" for u, e in failed[:3]) or "none stored"))
    else:
        res = "PASS" if not failed else "UNCLEAR"
        ev = f"Re-opened and read {len(docs)} page(s): " + ", ".join(list(docs)[:3]) + (f". Could not read: {', '.join(u for u, _ in failed[:2])}" if failed else "")
        out.append(_item("source_opened", "Stored source pages can be re-opened", res, ev, first_url))

    if not docs:   # nothing was read: every content check is UNCLEAR by definition, never PASS
        for k in ("headline", "figures", "names", "quotes", "recycled", "satire_rumour", "allegation"):
            out.append(_item(k, LABELS[k], "UNCLEAR", "Not tested: no source text was read."))
        out += _advisory(conn, s, items, docs, now, alltext, head)
        return out

    # 2. headline supported
    words = {w for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", head.lower()) if w not in STOP}
    low = masked.lower()
    hit = sorted(w for w in words if w in low)
    ratio = len(hit) / len(words) if words else 0
    best = _extract(alltext, re.compile(re.escape(hit[0]), re.I)) if hit else ""
    out.append(_item("headline", head, "PASS" if ratio >= 0.6 else ("UNCLEAR" if ratio >= 0.35 else "FAIL"),
                     f"{len(hit)} of {len(words)} key terms of the headline appear in the source text. " + (f"Example: {best}" if best else ""), first_url))

    # 3. figures
    figs = extract_figures(head + " " + summ)
    if not figs:
        out.append(_item("figures", "No figures in headline or excerpt", "N/A"))
    for f in figs:
        ok = figure_in_text(f, masked)
        m = re.search(re.escape(re.sub(r"[^\d.,]", "", f) or f), masked)
        out.append(_item("figures", f'Figure "{f}"', "PASS" if ok else "FAIL",
                         (_extract(alltext, re.compile(re.escape(m.group(0)))) if (ok and m) else "Figure not found in any page read."), first_url))

    # 4. names
    hw = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", head) if len(w) > 3]
    title_case = bool(hw) and sum(w[0].isupper() for w in hw) / len(hw) >= 0.6
    names = [] if title_case else list(dict.fromkeys(NAME.findall(head)))[:6]
    if title_case:
        out.append(_item("names", "Headline is in Title Case, so names cannot be told from ordinary words. Names stay a MANUAL claim", "N/A"))
    elif not names:
        out.append(_item("names", "No multi-word names in the headline", "N/A"))
    for n in names:
        ok = n.lower() in low
        out.append(_item("names", f'Name "{n}" (appears in text; real-world identity still needs a human)', "PASS" if ok else "UNCLEAR",
                         _extract(alltext, re.compile(re.escape(n), re.I)) if ok else "Name not found verbatim in the pages read (spelling or wording may differ).", first_url))

    # 5. quotes
    qs = extract_quotes(head + " " + summ)
    if not qs:
        out.append(_item("quotes", "No quotations in headline or excerpt", "N/A"))
    for q in qs:
        ok = _norm(q).lower() in low
        out.append(_item("quotes", f'Quotation "{q[:80]}"', "PASS" if ok else "FAIL",
                         _extract(alltext, re.compile(re.escape(_norm(q)), re.I)) if ok else "Quotation not found verbatim in the pages read.", first_url))

    # 6. recycled / old story
    pub = parse_iso(s["published_at"]) if s.get("published_at") else None
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", alltext)]
    if pub is None:
        out.append(_item("recycled", "Publication time known", "UNCLEAR", "The feed gave no publication time, so freshness cannot be confirmed."))
    else:
        age = now - pub
        if age > timedelta(days=3):
            out.append(_item("recycled", "Story is recent", "UNCLEAR", f"Feed says published {s['published_at']} ({age.days} days ago): may be old or updated material.", first_url))
        elif years and max(years) < now.year:
            out.append(_item("recycled", "Story is recent", "UNCLEAR", f"Page mentions no {now.year} date; latest year mentioned is {max(years)}.", first_url))
        else:
            out.append(_item("recycled", "Story is recent", "PASS", f"Published {s['published_at']} per the feed; page mentions {now.year}." if now.year in years else f"Published {s['published_at']} per the feed.", first_url))

    # 7. satire / rumour
    sat, rum = SATIRE.search(masked), RUMOUR.search(masked)
    if sat:
        out.append(_item("satire_rumour", "Not satire", "FAIL", _extract(alltext, SATIRE), first_url))
    elif rum:
        out.append(_item("satire_rumour", "Not rumour", "UNCLEAR", _extract(alltext, RUMOUR), first_url))
    else:
        out.append(_item("satire_rumour", "Not satire or rumour", "PASS", f"No satire or rumour markers in the {len(docs)} page(s) read."))

    # 8. allegation vs established fact
    est, alg = ESTAB.search(masked), ALLEG.search(masked)
    hedged = bool(HEDGE.search(head))
    if alg and not est and not hedged:
        out.append(_item("allegation", "Headline wording matches what the source establishes", "UNCLEAR",
                         "Source uses allegation language but the headline states it plainly: " + _extract(alltext, ALLEG), first_url))
    elif alg and not est:
        out.append(_item("allegation", "Headline wording matches what the source establishes", "PASS",
                         "Headline is hedged and the source frames it as allegation: " + _extract(alltext, ALLEG), first_url))
    elif est:
        out.append(_item("allegation", "Headline wording matches what the source establishes", "PASS",
                         "Source reports an outcome (stated by the source itself): " + _extract(alltext, ESTAB), first_url))
    else:
        out.append(_item("allegation", "Headline wording matches what the source establishes", "PASS", "No allegation language found in the pages read."))

    out += _advisory(conn, s, items, docs, now, alltext, head, bool(alg or est))
    return out


def _advisory(conn, s, items, docs, now, alltext, head, legal=False) -> list[dict]:
    out = []
    # dates
    dates = re.findall(r"\b\d{1,2}\s+(?:%s)[a-z]*\b|\b(?:%s)[a-z]*\.?\s+\d{1,2}\b" % ("|".join(MONTHS), "|".join(MONTHS)), head, re.I)
    if not dates or not docs:
        out.append(_item("dates", "No dates in the headline", "N/A"))
    for d in dates[:3]:
        ok = d.lower() in alltext.lower()
        out.append(_item("dates", f'Date "{d}"', "PASS" if ok else "UNCLEAR", _extract(alltext, re.compile(re.escape(d), re.I)) if ok else "Date not found verbatim (format may differ)."))
    # denials
    if docs:
        dn = DENIAL.search(alltext)
        if dn:
            out.append(_item("denials", "A denial or response appears in the source", "PASS", _extract(alltext, DENIAL)))
        elif legal:
            out.append(_item("denials", "A denial or response appears in the source", "UNCLEAR", "No denial or response found in the pages read: seek comment before publishing."))
        else:
            out.append(_item("denials", "No allegation, so no response required", "N/A"))
        cm = CONTRA.search(alltext)
        out.append(_item("contradiction", "Contradicting material", "UNCLEAR" if cm else "PASS",
                         _extract(alltext, CONTRA) if cm else f"None detected in the {len(docs)} page(s) read. Limited to wording cues; other outlets' versions still need a human read."))
    else:
        out.append(_item("denials", "Denial or response", "UNCLEAR", "Not tested: no source text was read."))
        out.append(_item("contradiction", "Contradicting material", "UNCLEAR", "Not tested: no source text was read."))
    # corroboration (count)
    prim_org = (s.get("source_org") or "") if s.get("primary_url") else ""
    outlets = {i["org"] for i in items if i["bot"] != "trends" and i["org"] != prim_org}
    n = len(outlets)
    res = "PASS" if n >= 2 else ("UNCLEAR" if (n == 1 or s.get("primary_url")) else "FAIL")
    out.append(_item("corroboration", f"Independent outlets reporting it: {n}", res,
                     (", ".join(sorted(outlets)) if outlets else "None besides the original source.") +
                     " (headline-level match only: outlets may share one wire story)." if outlets else "None besides the original source. Single-source."))
    return out


# ---------------------------------------------------------------- storage and gate
def store(conn, sid: int, results: list[dict], now, by: str = "system", docs_read: int = 0) -> int:
    cur = conn.execute("INSERT INTO second_checks(story_id,run_at,by,docs_read) VALUES(?,?,?,?)", (sid, iso(now), by, docs_read))
    rid = cur.lastrowid
    for r in results:
        conn.execute("INSERT INTO second_check_items(run_id,story_id,key,label,claim,result,evidence,source_url,material) VALUES(?,?,?,?,?,?,?,?,?)",
                     (rid, sid, r["key"], r["label"], r["claim"], r["result"], r["evidence"], r.get("source_url", ""), int(r["material"])))
    conn.commit()
    return rid


def run(conn, sid: int, now, get=http_get) -> dict:
    docs, failed = open_pages(conn, sid, get)
    rid = store(conn, sid, run_checks(conn, sid, now, docs, failed), now, "system", len(docs))
    return summary(conn, sid)


def latest_run(conn, sid: int):
    return conn.execute("SELECT * FROM second_checks WHERE story_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()


def _final(row) -> str:
    return row["resolved_result"] or row["result"]


def summary(conn, sid: int) -> dict:
    r = latest_run(conn, sid)
    if not r:
        return {"ran": False, "overall": "NOT RUN", "at": None, "items": [], "fail": 0, "unclear": 0}
    items = [dict(x) for x in conn.execute("SELECT * FROM second_check_items WHERE run_id=? ORDER BY id", (r["id"],))]
    for i in items:
        i["final"] = i["resolved_result"] or i["result"]
    mf = sum(1 for i in items if i["material"] and i["final"] == "FAIL")
    mu = sum(1 for i in items if i["material"] and i["final"] == "UNCLEAR")
    overall = "FAIL" if mf else ("UNCLEAR" if mu else "PASS")
    return {"ran": True, "overall": overall, "at": r["run_at"], "by": r["by"], "run_id": r["id"], "docs_read": r["docs_read"],
            "items": items, "fail": mf, "unclear": mu}


def gate(conn, sid: int) -> tuple[bool, str]:
    """Approval gate: a second check must exist and have no unresolved material FAIL/UNCLEAR."""
    sm = summary(conn, sid)
    if not sm["ran"]:
        return False, "second check not run (send /secondcheck ID)"
    if sm["overall"] != "PASS":
        return False, f"second check {sm['overall']}: {sm['fail']} material FAIL, {sm['unclear']} material UNCLEAR"
    return True, "ok"


def resolve(conn, sid: int, key: str, result: str, note: str, now, index: int = 1) -> bool:
    """A human settles a check that automation could not. Recorded with note and time; the original result is kept."""
    result = result.upper()
    r = latest_run(conn, sid)
    if not r or result not in ("PASS", "FAIL", "UNCLEAR") or len(note.strip()) < 3:
        return False
    rows = conn.execute("SELECT id FROM second_check_items WHERE run_id=? AND key=? ORDER BY id", (r["id"], key)).fetchall()
    if len(rows) < index or index < 1:
        return False
    conn.execute("UPDATE second_check_items SET resolved_result=?, resolved_note=?, resolved_at=? WHERE id=?", (result, note[:300], iso(now), rows[index - 1]["id"]))
    conn.commit()
    return True


# ---------------------------------------------------------------- presentation
ICON = {"PASS": "✅", "FAIL": "❌", "UNCLEAR": "❔", "N/A": "➖"}


def telegram_text(conn, sid: int, esc=lambda x: x) -> str:
    sm = summary(conn, sid)
    if not sm["ran"]:
        return f"Second check has not been run for #{sid}."
    L = [f"🔎 Second check #{sid}: {sm['overall']} ({sm['fail']} material fail, {sm['unclear']} unclear) · read {sm['docs_read']} page(s) · {sm['at']}"]
    for i in sm["items"]:
        if i["final"] == "N/A":
            continue
        tag = "" if i["material"] else " (advisory)"
        ev = (i["evidence"] or "").replace("\n", " ")[:120]
        res = f" → HUMAN {i['resolved_result']}: {i['resolved_note'][:50]}" if i["resolved_result"] else ""
        L.append(f"{ICON.get(i['final'], '?')} {esc(i['label'])}{tag}: {esc(i['claim'][:60])}\n   {esc(ev)}{esc(res)}")
    ok, why = gate(conn, sid)
    L.append("\n" + ("✅ Second check allows approval." if ok else f"⛔ Approval blocked: {why}. Resolve with /scresolve {sid} KEY PASS|FAIL note"))
    return "\n".join(L)[:3900]


def part_e_block(conn, sid: int) -> str:
    sm = summary(conn, sid)
    if not sm["ran"]:
        return "SECOND CHECK: NOT RUN. Approval is blocked until /secondcheck has been run and passes."
    L = [f"SECOND CHECK: {sm['overall']} at {sm['at']} ({sm['by']}; {sm['docs_read']} page(s) re-read; {sm['fail']} material FAIL, {sm['unclear']} material UNCLEAR)"]
    for i in sm["items"]:
        if i["final"] != "N/A":
            L.append(f"- [{i['final']}] {'MATERIAL' if i['material'] else 'advisory'} · {i['label']}: {i['claim'][:90]} | {(i['evidence'] or '')[:160]}"
                     + (f" | HUMAN-RESOLVED {i['resolved_result']} {i['resolved_at']}: {i['resolved_note']}" if i["resolved_result"] else ""))
    return "\n".join(L)


MARK = "<!-- SECOND-CHECK -->"


def write_part_e(conn, sid: int, data_dir: str) -> bool:
    row = conn.execute("SELECT package_path FROM stories WHERE id=?", (sid,)).fetchone()
    if not row or not row["package_path"]:
        return False
    p = os.path.join(data_dir, row["package_path"], "05_part_e_report.md")
    if not os.path.exists(p):
        return False
    t = open(p, encoding="utf-8").read().split(MARK)[0].rstrip()
    open(p, "w", encoding="utf-8").write(t + "\n\n" + MARK + "\n" + part_e_block(conn, sid) + "\n")
    return True


# ---------------------------------------------------------------- results submitted by a Claude Code session (anti-fabrication)
def accept_submitted(conn, sid: int, items: list[dict], now, get=http_get, by: str = "claude-skill") -> dict:
    """A Claude Code skill may submit judgements, but every evidence extract is checked against the re-fetched page.
    If the extract is not literally on the page, the item is downgraded to UNCLEAR. Nothing is taken on trust."""
    docs, failed = open_pages(conn, sid, get)
    low = {u: t.lower() for u, t in docs.items()}
    out, downgraded = [], 0
    for it in items:
        key = it.get("key")
        if key not in LABELS:
            continue
        res = str(it.get("result", "UNCLEAR")).upper()
        ev = _norm(str(it.get("evidence", "")))
        src = it.get("source_url", "")
        ok_ev = bool(ev) and any(_norm(re.sub(r"^…|…$", "", ev)).lower() in t for t in low.values())
        note = ""
        if res in ("PASS", "FAIL") and not ok_ev and res != "N/A":
            res, note, downgraded = "UNCLEAR", "Submitted evidence extract was NOT found on the re-fetched page; downgraded. ", downgraded + 1
        if not docs:
            res, note = "UNCLEAR", "No source page could be opened. "
        out.append(_item(key, str(it.get("claim", LABELS[key])), res, (note + ev)[:300], src))
    rid = store(conn, sid, out, now, by, len(docs))
    return {"ok": True, "run_id": rid, "downgraded": downgraded, "pages_read": len(docs)}
