"""Source-link integrity.

Every claim must point at a stored SOURCE RECORD: original URL, publisher, date published, date retrieved and an archived copy
(archive.org capture, or a full copy of an official public record). Before a story can be approved, an automatic LINK CHECK
re-opens every cited link, confirms the page still contains the cited text, and confirms the archived copy exists.
Approval is blocked if any claim has no source or any link fails. Published stories are re-checked weekly (see `weekly`).

The website's links and evidence items are generated ONLY from these records (see siteexport.source_records); nothing is typed by hand.
Deterministic: no AI. Network access goes through the injected `get` / `wb` callables so tests never touch the internet."""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from . import evidence, secondcheck
from .fetch import FetchError, http_get
from .util import iso, parse_iso, strip_html

CITE_MIN = 0.8          # share of the cited words that must still be on the page
FRESH_MINUTES = 60      # a passing approval-time check is valid for this long (approve right after it)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:['’.-][a-z0-9]+)*", (text or "").lower())


def coverage(cite: str, page_text: str) -> float:
    """Share of the cited text's words found on the page (1.0 when there is nothing to check)."""
    w = _words(cite)
    if not w:
        return 1.0
    page = " " + " ".join(_words(page_text)) + " "
    if len(w) <= 3:
        return 1.0 if " " + " ".join(w) + " " in page else 0.0
    have = set(page.split())
    return sum(1 for x in w if x in have) / len(w)


def claims_with_urls(conn, sid: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM claims WHERE story_id=? ORDER BY id", (sid,))]


def claims_sig(conn, sid: int) -> str:
    rows = [(c["id"], c["source_url"] or "", c.get("cite_text") or "") for c in claims_with_urls(conn, sid)]
    return hashlib.sha256(repr(rows).encode()).hexdigest()[:16]


def unsourced(conn, sid: int) -> list[dict]:
    return [c for c in claims_with_urls(conn, sid) if not (c["source_url"] or "").strip()]


def cite_for(conn, sid: int, claim: dict) -> str:
    """The text we expect to find on the cited page: the claim's own quoted text, else the headline the source published."""
    if (claim.get("cite_text") or "").strip():
        return claim["cite_text"]
    it = conn.execute("SELECT title FROM items WHERE story_id=? AND url=? LIMIT 1", (sid, claim["source_url"])).fetchone()
    if it and it["title"]:
        return it["title"]
    return conn.execute("SELECT headline FROM stories WHERE id=?", (sid,)).fetchone()["headline"]


def record_for(conn, sid: int, url: str) -> dict | None:
    """The newest stored source record for this URL (preferring one that already has an archived copy)."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM evidence_archive WHERE story_id=? AND url=? ORDER BY id DESC", (sid, url))]
    for r in rows:
        if r["http_status"] == 200 and (r["wayback_url"] or r["full_copy_path"]):
            return r
    return rows[0] if rows else None


def _page_text(r) -> str:
    return strip_html(secondcheck.clean_html(secondcheck.decode_text(r)), limit=400_000)


def _reasons_for(status, fetched, cov, rec, override) -> list[str]:
    out = []
    if not fetched:
        out.append(f"the page did not open ({status})")
    elif cov < CITE_MIN:
        out.append("the cited text is not on the page")
    if not rec:
        out.append("no stored source record")
    else:
        if not (rec.get("wayback_url") or rec.get("full_copy_path")):
            out.append("no archived copy")
        if not (rec.get("issuer") or "").strip():
            out.append("publisher missing")
        if not (rec.get("published_at") or "").strip():
            out.append("date published missing")
    return [] if override else out


def run(conn, sid: int, now, data_dir: str = "data", cfg=None, get=http_get, wb=None, kind: str = "approval") -> dict:
    """Open every cited link, check the cited text and the archived copy. Records the run; returns {ok, rows}."""
    if cfg is None:
        from .config import load_all
        cfg = load_all()
    if wb is None:      # an injected fetcher means "offline": never call archive.org from tests
        wb = evidence.wayback if get is http_get else (lambda u: ("", "offline"))
    claims = claims_with_urls(conn, sid)
    fetched: dict[str, tuple] = {}
    rows = []
    for c in claims:
        url = (c["source_url"] or "").strip()
        if not url:
            rows.append({"claim_id": c["id"], "url": "", "ok": False, "status": None, "found": 0, "archived": 0, "reasons": ["this fact has no source"]})
            continue
        if url not in fetched:
            try:
                r = get(url, timeout=25, retries=1)
                fetched[url] = (r.status_code, _page_text(r), True)
            except FetchError as ex:
                fetched[url] = (f"HTTP {ex.status}" if ex.status else "no response", "", False)
            except Exception as ex:  # noqa: BLE001
                fetched[url] = (type(ex).__name__, "", False)
            rec = record_for(conn, sid, url)
            if kind == "approval" and fetched[url][2] and not (rec and (rec["wayback_url"] or rec["full_copy_path"])):
                try:
                    evidence.archive_story(conn, sid, now, data_dir, cfg, get=get, wb=wb, urls_override=[url])
                except Exception:  # noqa: BLE001
                    pass
        status, text, opened = fetched[url]
        rec = record_for(conn, sid, url)
        cov = coverage(cite_for(conn, sid, c), text) if opened else 0.0
        anchor_ok = True
        if kind == "weekly" and opened and rec and rec.get("extract"):
            anchor_ok = coverage(rec["extract"], text) >= CITE_MIN        # the passage we archived is still there
        override = (c.get("link_override") or "").strip()
        reasons = _reasons_for(status, opened, cov, rec, override)
        if kind == "weekly" and not anchor_ok and not override and not reasons:
            reasons = ["the source page has changed since we archived it"]
        rows.append({"claim_id": c["id"], "url": url, "ok": not reasons, "status": status if not isinstance(status, int) else status, "found": int(cov >= CITE_MIN),
                     "archived": int(bool(rec and (rec.get("wayback_url") or rec.get("full_copy_path")))), "reasons": reasons,
                     "override": override})
    ok = bool(rows) and all(r["ok"] for r in rows)
    cur = conn.execute("INSERT INTO link_runs(story_id,at,kind,ok,claims_sig) VALUES(?,?,?,?,?)", (sid, iso(now), kind, int(ok), claims_sig(conn, sid)))
    for r in rows:
        conn.execute("INSERT INTO link_checks(run_id,story_id,claim_id,url,ok,http_status,text_found,archived,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                     (cur.lastrowid, sid, r["claim_id"], r["url"], int(r["ok"]), str(r["status"]) if r["status"] is not None else None,
                      r["found"], r["archived"], "; ".join(r["reasons"]) or (f"manually verified: {r['override']}" if r.get("override") else "ok")))
    conn.commit()
    return {"ok": ok, "rows": rows, "run_id": cur.lastrowid}


def store_pass(conn, sid: int, now, by: str = "test") -> None:
    """Record a passing approval-time check for the CURRENT claims. Used by the self-test and the unit tests only."""
    cur = conn.execute("INSERT INTO link_runs(story_id,at,kind,ok,claims_sig) VALUES(?,?,?,?,?)", (sid, iso(now), "approval", 1, claims_sig(conn, sid)))
    for c in claims_with_urls(conn, sid):
        conn.execute("INSERT INTO link_checks(run_id,story_id,claim_id,url,ok,http_status,text_found,archived,reason) VALUES(?,?,?,?,1,'200',1,1,?)",
                     (cur.lastrowid, sid, c["id"], c["source_url"] or "", f"{by}: assumed pass"))
    conn.commit()


def latest_run(conn, sid: int, kind: str = "approval"):
    return conn.execute("SELECT * FROM link_runs WHERE story_id=? AND kind=? ORDER BY id DESC LIMIT 1", (sid, kind)).fetchone()


def summary(conn, sid: int) -> dict:
    r = latest_run(conn, sid)
    if not r:
        return {"ran": False, "ok": False, "failed": [], "at": None}
    bad = [dict(x) for x in conn.execute("SELECT * FROM link_checks WHERE run_id=? AND ok=0 ORDER BY id", (r["id"],))]
    return {"ran": True, "ok": bool(r["ok"]), "failed": bad, "at": r["at"], "sig": r["claims_sig"]}


def gate(conn, sid: int, now=None) -> tuple[bool, str]:
    """Approval gate: every claim has a source, and a recent, passing link check covers exactly the current claims."""
    n = len(unsourced(conn, sid))
    if n:
        return False, f"{n} fact(s) have no source link"
    sm = summary(conn, sid)
    if not sm["ran"]:
        return False, "links not checked yet"
    if sm["sig"] != claims_sig(conn, sid):
        return False, "the facts changed since the links were checked"
    if not sm["ok"]:
        if not sm["failed"]:
            return False, "no facts with source links are recorded yet, so there is nothing to check"
        f = sm["failed"][0]
        return False, f"{len(sm['failed'])} link(s) failed (e.g. {f['url'][:60] or 'a fact with no link'}: {f['reason']})"
    if now is not None and (now - parse_iso(sm["at"])) > timedelta(minutes=FRESH_MINUTES):
        return False, "the link check is more than an hour old; run it again"
    return True, "ok"


def resolve(conn, sid: int, claim_id: int, note: str, now) -> bool:
    """A human opened the link themselves (e.g. a site that refuses automated checks). Recorded with a note; the claim then passes the link check."""
    if len(note.strip()) < 3:
        return False
    n = conn.execute("UPDATE claims SET link_override=? WHERE id=? AND story_id=?", (f"{note.strip()[:200]} ({iso(now)[:10]})", claim_id, sid)).rowcount
    conn.commit()
    return n > 0


# ---------------------------------------------------------------- weekly re-check of published stories
def weekly(conn, notifier, now, data_dir: str = "data", cfg=None, get=http_get) -> dict:
    """Re-open every cited link on approved/published stories. A broken link or a changed page triggers a Telegram alert naming the story and link."""
    from . import editorial as ed
    stories = [dict(r) for r in conn.execute("SELECT id, headline FROM stories WHERE editorial_status IN ('approved','published') AND lead_only=0 ORDER BY id")]
    week = now.strftime("%G-W%V")
    total = bad = alerts = 0
    for s in stories:
        res = run(conn, s["id"], now, data_dir, cfg, get, kind="weekly")
        for r in res["rows"]:
            total += 1
            if r["ok"]:
                continue
            bad += 1
            key = f"linkwatch:{ed.uid(s['id'])}:{hashlib.sha1((r['url'] or str(r['claim_id'])).encode()).hexdigest()[:10]}:{week}"
            text = (f"🔗 <b>LINK PROBLEM</b> · {ed.uid(s['id'])}\n{_esc(s['headline'])}\nLink: {_esc(r['url'] or '(a fact with no link)')}\n"
                    f"What happened: {_esc('; '.join(r['reasons']))}.\nOpen the story, check the source, and correct the story if it is now wrong.")
            if notifier.send_once(key, "linkwatch", text, now, story_id=s["id"]) == "sent":
                alerts += 1
    summary_text = (f"🔗 Weekly link check: {total} link(s) on {len(stories)} published story(ies), "
                    + ("all still open and unchanged." if not bad else f"{bad} problem(s), {alerts} alert(s) sent above."))
    notifier.send_once(f"linkwatch-summary:{week}", "linkwatch", summary_text, now)
    return {"stories": len(stories), "links": total, "problems": bad, "alerts": alerts}


def _esc(t) -> str:
    import html
    return html.escape(str(t or ""), quote=False)
