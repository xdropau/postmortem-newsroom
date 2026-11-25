"""Editorial layer inside the SAME database: version-specific approvals, corrections, investigative leads, breaking flag,
live coverage, story-to-asset links, latency metrics, evidence export, and the drafts inbox.
Nothing here publishes anything. Publication stays a manual act that this layer can only GATE."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import statistics
import subprocess
from datetime import timedelta

from .util import iso, parse_iso
from .verify import can_approve, refresh_status

KINDS = ("narration", "article", "breaking_article", "captions", "visuals", "seo", "newsletter")
LEAD_STATUSES = ("new", "researching", "needs-evidence", "response-requested", "verified", "converted", "dropped")
LIVE_STATES = ("LIVE", "PAUSED", "CONCLUDED")
CLASSIFICATIONS = ("VERIFIED", "COMPANY-REPORTED", "INDEPENDENTLY CORROBORATED", "REPORTED ALLEGATION", "DISPUTED", "UNVERIFIED")
MAX_BREAKING_HOURS = 24
CLOSING = ("This is The Post-Mortem. Documents. Money. Evidence.\n"
           "If you want us to keep following the documents, the money, and the evidence, subscribing is the simplest way "
           "to help us keep doing that.\nThank you for watching.")


def uid(story_id: int) -> str:
    """The one persistent story ID shared by story, article, video, social posts, notifications and corrections."""
    return f"PM-{int(story_id):06d}"


def _norm(text: str) -> str:
    return re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n")).strip()


def _hash(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def _story(conn, sid):
    return conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()


# ---------------------------------------------------------------- versions and approvals
def save_version(conn, sid: int, kind: str, content: str, now, author: str = "claude-draft") -> dict:
    """Store a content version. Any material change creates a NEW version and invalidates every approval of this kind."""
    if kind not in KINDS:
        return {"ok": False, "error": f"kind must be one of {KINDS}"}
    if not _story(conn, sid):
        return {"ok": False, "error": f"no story {sid}"}
    h = _hash(content)
    last = conn.execute("SELECT * FROM content_versions WHERE story_id=? AND kind=? ORDER BY version DESC LIMIT 1", (sid, kind)).fetchone()
    if last and last["content_hash"] == h:
        return {"ok": True, "changed": False, "version": last["version"], "invalidated": 0}
    v = (last["version"] if last else 0) + 1
    conn.execute("INSERT INTO content_versions(story_id,kind,version,content,content_hash,created_at,author) VALUES(?,?,?,?,?,?,?)",
                 (sid, kind, v, _norm(content), h, iso(now), author))
    inv = conn.execute("UPDATE approvals SET valid=0, invalidated_at=?, invalidated_reason=? "
                       "WHERE story_id=? AND kind=? AND action='approve' AND valid=1",
                       (iso(now), f"{kind} edited: v{v} supersedes it", sid, kind)).rowcount
    if inv:
        conn.execute("INSERT INTO approvals(story_id,kind,version,content_hash,action,reason,at,valid) VALUES(?,?,?,?,?,?,?,0)",
                     (sid, kind, v, h, "invalidated", f"{inv} approval(s) voided by edit", iso(now)))
    if kind == "narration":
        conn.execute("UPDATE stories SET script=? WHERE id=?", (_norm(content), sid))
    if kind in ("article", "breaking_article"):
        conn.execute("UPDATE stories SET article_draft=? WHERE id=?", (_norm(content), sid))
    _sync_story_status(conn, sid)
    conn.commit()
    return {"ok": True, "changed": True, "version": v, "invalidated": inv}


def latest_versions(conn, sid: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT cv.* FROM content_versions cv JOIN (SELECT kind, MAX(version) mv FROM content_versions WHERE story_id=? GROUP BY kind) m "
        "ON cv.kind=m.kind AND cv.version=m.mv WHERE cv.story_id=? ORDER BY cv.kind", (sid, sid))]


def _is_approved(conn, ver: dict, dest: str | None = None) -> bool:
    q = ("SELECT 1 FROM approvals WHERE story_id=? AND kind=? AND version=? AND content_hash=? AND action='approve' AND valid=1")
    a = [ver["story_id"], ver["kind"], ver["version"], ver["content_hash"]]
    if dest:
        q += " AND destination IN (?, 'all')"
        a.append(dest)
    return conn.execute(q, a).fetchone() is not None


def _sync_story_status(conn, sid: int):
    s = _story(conn, sid)
    if s["editorial_status"] in ("rejected", "published"):
        return
    vers = latest_versions(conn, sid)
    allok = bool(vers) and all(_is_approved(conn, v) for v in vers)
    ok_claims, _ = can_approve(conn, sid)
    if allok and ok_claims:
        new = "approved"
    elif s["editorial_status"] == "approved":
        new = "ready" if ok_claims else "verifying"   # an edit or a reopened claim drops it out of "approved"
    else:
        return
    conn.execute("UPDATE stories SET editorial_status=? WHERE id=?", (new, sid))


def approve(conn, sid: int, now, kind: str | None = None, destination: str = "all", by: str = "owner") -> dict:
    """Approve the LATEST version of one kind (or all kinds). Approval is bound to the version's content hash."""
    ok, out = can_approve(conn, sid)
    if not ok:
        return {"ok": False, "error": "verification incomplete", "outstanding": out}
    vers = latest_versions(conn, sid)
    if kind:
        vers = [v for v in vers if v["kind"] == kind]
    if not vers:
        return {"ok": False, "error": "no saved draft content to approve; prepare the story first"}
    done = []
    for v in vers:
        if not _is_approved(conn, v, destination):
            conn.execute("INSERT INTO approvals(story_id,kind,version,content_hash,action,destination,reason,at,valid) VALUES(?,?,?,?,?,?,?,?,1)",
                         (sid, v["kind"], v["version"], v["content_hash"], "approve", destination, by, iso(now)))
        done.append(f"{v['kind']} v{v['version']}")
    _sync_story_status(conn, sid)
    conn.commit()
    return {"ok": True, "approved": done, "status": _story(conn, sid)["editorial_status"]}


def reject(conn, sid: int, now, reason: str = "") -> dict:
    conn.execute("UPDATE approvals SET valid=0, invalidated_at=?, invalidated_reason='story rejected' WHERE story_id=? AND action='approve' AND valid=1",
                 (iso(now), sid))
    conn.execute("INSERT INTO approvals(story_id,kind,action,reason,at,valid) VALUES(?,?,?,?,?,0)", (sid, "story", "reject", reason[:500], iso(now)))
    conn.execute("UPDATE stories SET editorial_status='rejected' WHERE id=?", (sid,))
    conn.commit()
    return {"ok": True}


def request_research(conn, sid: int, now, note: str) -> dict:
    conn.execute("INSERT INTO approvals(story_id,kind,action,reason,at,valid) VALUES(?,?,?,?,?,0)", (sid, "story", "research_requested", note[:500], iso(now)))
    conn.execute("UPDATE stories SET editorial_status='verifying' WHERE id=? AND editorial_status IN ('new','ready','approved')", (sid,))
    conn.commit()
    return {"ok": True}


def can_publish(conn, sid: int, destination: str | None = None) -> tuple[bool, str]:
    """The only gate to 'published': every latest version validly approved (for this destination) and no open verification."""
    s = _story(conn, sid)
    if not s:
        return False, "no such story"
    if s["editorial_status"] == "rejected":
        return False, "story is rejected"
    ok, out = can_approve(conn, sid)
    if not ok:
        return False, "verification incomplete"
    vers = latest_versions(conn, sid)
    if not vers:
        return False, "no draft content has been saved and approved"
    stale = [v["kind"] for v in vers if not _is_approved(conn, v, destination)]
    if stale:
        return False, "latest version not approved: " + ", ".join(stale)
    return True, "ok"


def mark_published(conn, sid: int, now, destination: str = "website") -> dict:
    ok, why = can_publish(conn, sid, destination)
    if not ok:
        return {"ok": False, "error": why}
    conn.execute("UPDATE stories SET editorial_status='published', publication_status='published' WHERE id=?", (sid,))
    conn.execute("INSERT INTO approvals(story_id,kind,action,destination,at,valid) VALUES(?,?,?,?,?,0)", (sid, "story", "published", destination, iso(now)))
    conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- corrections
def add_correction(conn, sid: int, text: str, now, severity: str = "clarification", fixes_version: int | None = None) -> dict:
    if not _story(conn, sid) or not text.strip():
        return {"ok": False, "error": "need a story and correction text"}
    cur = conn.execute("INSERT INTO corrections(story_id,text,severity,fixes_version,created_at,status) VALUES(?,?,?,?,?,'open')",
                       (sid, text.strip()[:2000], severity, fixes_version, iso(now)))
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


def close_correction(conn, cid: int, now, note: str = "") -> bool:
    n = conn.execute("UPDATE corrections SET status='published', published_at=?, note=? WHERE id=?", (iso(now), note[:300], cid)).rowcount
    conn.commit()
    return n > 0


# ---------------------------------------------------------------- investigative leads
def add_lead(conn, now, title: str, story_id: int | None = None, **f) -> dict:
    """A lead is a hypothesis to research, never a finding. Originality is NOT CHECKED until a human sets it."""
    if not title.strip():
        return {"ok": False, "error": "title required"}
    cur = conn.execute(
        "INSERT INTO leads(story_id,title,documents,significance,angle,alternatives,missing_evidence,next_steps,parties,status,originality,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (story_id, title.strip()[:300], f.get("documents", ""), f.get("significance", ""), f.get("angle", ""), f.get("alternatives", ""),
         f.get("missing_evidence", ""), f.get("next_steps", ""), f.get("parties", ""), "new", "NOT CHECKED", iso(now), iso(now)))
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


def set_lead(conn, lead_id: int, now, **f) -> bool:
    allowed = {"title", "documents", "significance", "angle", "alternatives", "missing_evidence", "next_steps", "parties", "status", "originality", "story_id"}
    f = {k: v for k, v in f.items() if k in allowed}
    if "status" in f and f["status"] not in LEAD_STATUSES:
        return False
    if not f:
        return False
    sets = ", ".join(f"{k}=?" for k in f)
    n = conn.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?", [*f.values(), iso(now), lead_id]).rowcount
    conn.commit()
    return n > 0


# ---------------------------------------------------------------- breaking flag (auto-expiring)
def set_breaking(conn, sid: int, now, hours: float = 6) -> dict:
    hours = max(0.25, min(float(hours), MAX_BREAKING_HOURS))
    until = iso(now + timedelta(hours=hours))
    conn.execute("UPDATE stories SET breaking=1, breaking_until=? WHERE id=?", (until, sid))
    conn.commit()
    return {"ok": True, "until": until}


def clear_breaking(conn, sid: int) -> None:
    conn.execute("UPDATE stories SET breaking=0, breaking_until=NULL WHERE id=?", (sid,))
    conn.commit()


def expire_breaking(conn, now) -> int:
    n = conn.execute("UPDATE stories SET breaking=0 WHERE breaking=1 AND breaking_until IS NOT NULL AND breaking_until<=?", (iso(now),)).rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------- live coverage
def live_open(conn, sid: int, now, title: str = "") -> dict:
    row = conn.execute("SELECT * FROM live_coverage WHERE story_id=?", (sid,)).fetchone()
    if row:
        return {"ok": True, "id": row["id"], "existing": True}     # one authoritative page per event
    s = _story(conn, sid)
    if not s:
        return {"ok": False, "error": "no such story"}
    cur = conn.execute("INSERT INTO live_coverage(story_id,title,state,opened_at,state_changed_at) VALUES(?,?,?,?,?)",
                       (sid, title or s["headline"][:200], "LIVE", iso(now), iso(now)))
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "existing": False}


def live_state(conn, sid: int, state: str, now) -> bool:
    state = state.upper()
    if state not in LIVE_STATES:
        return False
    n = conn.execute("UPDATE live_coverage SET state=?, state_changed_at=? WHERE story_id=?", (state, iso(now), sid)).rowcount
    conn.commit()
    return n > 0


def live_add_update(conn, sid: int, text: str, now, source_url: str = "", classification: str = "UNVERIFIED", item_id: int | None = None) -> dict:
    cov = conn.execute("SELECT * FROM live_coverage WHERE story_id=?", (sid,)).fetchone()
    if not cov:
        return {"ok": False, "error": "no live coverage for this story"}
    classification = classification.upper().replace("_", " ")
    if classification not in CLASSIFICATIONS:
        classification = "UNVERIFIED"
    if item_id and conn.execute("SELECT 1 FROM live_updates WHERE coverage_id=? AND item_id=?", (cov["id"], item_id)).fetchone():
        return {"ok": True, "duplicate": True}
    cur = conn.execute("INSERT INTO live_updates(coverage_id,story_id,text,source_url,classification,status,item_id,created_at,content_hash) VALUES(?,?,?,?,?,'draft',?,?,?)",
                       (cov["id"], sid, text.strip()[:2000], source_url, classification, item_id, iso(now), _hash(text + source_url + classification)))
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


def live_edit_update(conn, upd_id: int, now, text: str | None = None, classification: str | None = None, source_url: str | None = None) -> bool:
    u = conn.execute("SELECT * FROM live_updates WHERE id=?", (upd_id,)).fetchone()
    if not u:
        return False
    t = text if text is not None else u["text"]
    c = (classification or u["classification"]).upper().replace("_", " ")
    s = source_url if source_url is not None else u["source_url"]
    h = _hash(t + s + c)
    if h != u["content_hash"]:   # material change: approval is voided
        conn.execute("UPDATE live_updates SET text=?, classification=?, source_url=?, content_hash=?, status='draft', approved_at=NULL, approved_hash=NULL WHERE id=?",
                     (t, c, s, h, upd_id))
        conn.commit()
    return True


def live_approve_update(conn, upd_id: int, now) -> dict:
    u = conn.execute("SELECT * FROM live_updates WHERE id=?", (upd_id,)).fetchone()
    if not u:
        return {"ok": False, "error": "no such update"}
    if not u["source_url"]:
        return {"ok": False, "error": "an update needs an original source link before approval"}
    conn.execute("UPDATE live_updates SET status='approved', approved_at=?, approved_hash=? WHERE id=?", (iso(now), u["content_hash"], upd_id))
    conn.commit()
    return {"ok": True}


def live_publishable(conn, sid: int) -> list[dict]:
    """Only updates whose approved hash still equals their current hash may appear publicly."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM live_updates WHERE story_id=? AND status='approved' AND approved_hash=content_hash ORDER BY created_at", (sid,))]


def discover_live_updates(conn, now) -> int:
    """New items attached to a story that has LIVE coverage become DRAFT updates (never public until approved)."""
    n = 0
    for cov in conn.execute("SELECT * FROM live_coverage WHERE state='LIVE'").fetchall():
        for it in conn.execute("SELECT * FROM items WHERE story_id=? AND bot!='trends' ORDER BY id", (cov["story_id"],)).fetchall():
            r = live_add_update(conn, cov["story_id"], f"[{it['org']}] {it['title']}", now, it["url"], "UNVERIFIED", it["id"])
            if r.get("ok") and not r.get("duplicate"):
                n += 1
    return n


# ---------------------------------------------------------------- one story ID across assets
def link_story(conn, sid: int, kind: str, ref: str, now) -> dict:
    """kind: article | video | social:<platform> | newsletter. One article / one video per story: re-linking a different one is refused."""
    if not _story(conn, sid):
        return {"ok": False, "error": "no such story"}
    if kind in ("article", "video"):
        ex = conn.execute("SELECT ref FROM story_links WHERE story_id=? AND kind=?", (sid, kind)).fetchone()
        if ex and ex["ref"] != ref:
            return {"ok": False, "error": f"story already has a {kind} ({ex['ref']}); refusing duplicate"}
    conn.execute("INSERT OR IGNORE INTO story_links(story_id,kind,ref,created_at) VALUES(?,?,?,?)", (sid, kind, ref, iso(now)))
    if kind == "article":
        conn.execute("UPDATE stories SET article_id=? WHERE id=?", (ref, sid))
    if kind == "video":
        conn.execute("UPDATE stories SET video_id=? WHERE id=?", (ref, sid))
    conn.commit()
    return {"ok": True, "story_uid": uid(sid)}


# ---------------------------------------------------------------- metrics
def _pct(vals, p):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]


def latency_metrics(conn) -> dict:
    """Source-to-detection latency per source organisation, from real timestamps only (items whose feed gave a publication time)."""
    per: dict[str, list[float]] = {}
    for r in conn.execute("SELECT org, published_at, detected_at FROM items WHERE pub_known=1 AND published_at IS NOT NULL"):
        d = (parse_iso(r["detected_at"]) - parse_iso(r["published_at"])).total_seconds() / 60
        if 0 <= d < 60 * 24 * 7:
            per.setdefault(r["org"], []).append(d)
    rows = [{"org": o, "n": len(v), "median_min": round(statistics.median(v), 1), "p90_min": round(_pct(v, 0.9), 1)} for o, v in per.items()]
    rows.sort(key=lambda x: x["median_min"])
    allv = [x for v in per.values() for x in v]
    out = {"source_to_detection": rows,
           "overall": {"n": len(allv), "median_min": round(statistics.median(allv), 1) if allv else None, "p90_min": round(_pct(allv, 0.9), 1) if allv else None}}
    stage = {}
    q = {"detection_to_first_draft_min": "SELECT s.detected_at a, MIN(v.created_at) b FROM stories s JOIN content_versions v ON v.story_id=s.id GROUP BY s.id",
         "draft_to_approval_min": "SELECT MIN(v.created_at) a, MIN(a.at) b FROM content_versions v JOIN approvals a ON a.story_id=v.story_id AND a.action='approve' GROUP BY v.story_id",
         "approval_to_publication_min": "SELECT MIN(a.at) a, MIN(p.at) b FROM approvals a JOIN approvals p ON p.story_id=a.story_id AND p.action='published' WHERE a.action='approve' GROUP BY a.story_id"}
    for k, sql in q.items():
        vals = [(parse_iso(r["b"]) - parse_iso(r["a"])).total_seconds() / 60 for r in conn.execute(sql) if r["a"] and r["b"]]
        stage[k] = {"n": len(vals), "median": round(statistics.median(vals), 1) if vals else None}
    out["stages"] = stage
    return out


def evidence_rows(conn, public_only: bool = False) -> list[dict]:
    """Evidence Library: one row per source document tied to a claim or a primary URL."""
    rows, seen = [], set()
    for r in conn.execute(
            "SELECT s.id sid, s.headline, s.editorial_status, s.primary_url, s.source_org, s.published_at spub, "
            "c.id cid, c.claim_text, c.status, c.source_url FROM stories s LEFT JOIN claims c ON c.story_id=s.id "
            "WHERE s.lead_only=0 ORDER BY s.id, c.id"):
        url = r["source_url"] or r["primary_url"]
        if not url or (r["sid"], url, r["status"]) in seen:
            continue
        seen.add((r["sid"], url, r["status"]))
        it = conn.execute("SELECT title, org, published_at FROM items WHERE story_id=? AND url=? LIMIT 1", (r["sid"], url)).fetchone()
        checked = conn.execute("SELECT http_status, opened_at FROM source_checks WHERE story_id=? AND url=? ORDER BY id DESC LIMIT 1", (r["sid"], url)).fetchone()
        public = r["editorial_status"] in ("approved", "published")
        if public_only and not public:
            continue
        rows.append({"story_id": uid(r["sid"]), "title": (it["title"] if it else r["headline"]), "issuer": (it["org"] if it else r["source_org"]),
                     "date": (it["published_at"] if it else r["spub"]) or "", "url": url,
                     "relevance": (r["claim_text"] or f"Primary source for: {r['headline']}")[:300],
                     "classification": r["status"] or "UNVERIFIED",
                     "opened_by_system": bool(checked and checked["http_status"] == 200), "public_eligible": public})
    return rows


def export_evidence(conn, out_dir: str, public_only: bool = False) -> dict:
    rows = evidence_rows(conn, public_only)
    os.makedirs(out_dir, exist_ok=True)
    cols = ["story_id", "title", "issuer", "date", "url", "relevance", "classification", "opened_by_system", "public_eligible"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    with open(os.path.join(out_dir, "evidence_library.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    with open(os.path.join(out_dir, "evidence_library.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
    return {"rows": len(rows), "dir": out_dir}


# ---------------------------------------------------------------- drafting checks (Part E and the closing line)
def check_narration(text: str) -> dict:
    t = _norm(text)
    has_close = t.endswith(CLOSING) or CLOSING in t
    body = t.split(CLOSING)[0] if CLOSING in t else t
    words = len(re.findall(r"\b[\w'’-]+\b", body))
    flags = []
    if not 170 <= words <= 190:
        flags.append(f"narration is {words} words (target 170-190, closing line excluded)")
    if not has_close:
        flags.append("closing line missing or altered (must be verbatim)")
    if re.search(r"\[[A-Za-z ]+\]|^#+ ", t, re.M):
        flags.append("contains [labels] or headers; must be paste-ready for ElevenLabs")
    if re.search(r"\bAI[- ]generated\b|\bAnthropic\b|\bClaude\b|x-?drop", t, re.I):
        flags.append("contains an AI/Anthropic/XDrop mention (not allowed in narration)")
    return {"words": words, "closing_ok": has_close, "flags": flags, "ok": not flags}


# ---------------------------------------------------------------- inbox: drafts submitted from the owner's machine
APPLY = {
    "save_version": lambda c, a, now: save_version(c, int(a["story_id"]), a["kind"], a["content"], now, a.get("author", "claude-draft")),
    "add_lead": lambda c, a, now: add_lead(c, now, a.pop("title"), a.pop("story_id", None), **a),
    "add_correction": lambda c, a, now: add_correction(c, int(a["story_id"]), a["text"], now, a.get("severity", "clarification")),
    "link_story": lambda c, a, now: link_story(c, int(a["story_id"]), a["kind"], a["ref"], now),
    "live_update": lambda c, a, now: live_add_update(c, int(a["story_id"]), a["text"], now, a.get("source_url", ""), a.get("classification", "UNVERIFIED")),
}


def apply_ops(conn, ops: list[dict], now) -> list[dict]:
    res = []
    for op in ops:
        fn = APPLY.get(op.get("op"))
        try:
            res.append(fn(conn, dict(op.get("args", {})), now) if fn else {"ok": False, "error": "unknown op"})
        except Exception as ex:  # noqa: BLE001 - a bad draft must never break the cycle
            res.append({"ok": False, "error": f"{type(ex).__name__}: {ex}"})
    return res


def process_inbox(conn, data_dir: str, now) -> int:
    """Apply drafts pushed by the owner to git branches inbox-* of the private data repo. Idempotent via kv markers."""
    from .db import kv_get, kv_set
    g = ["git", "-C", data_dir]
    if not os.path.isdir(os.path.join(data_dir, ".git")):
        return 0
    subprocess.run(g + ["fetch", "-q", "origin", "refs/heads/inbox-*:refs/remotes/origin/inbox-*"], capture_output=True)
    refs = subprocess.run(g + ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/inbox-*"], capture_output=True, text=True).stdout.split()
    applied = 0
    done = os.path.join(data_dir, ".inbox_done")
    for ref in refs:
        if kv_get(conn, f"inbox:{ref}"):
            continue
        blob = subprocess.run(g + ["show", f"{ref}:inbox.json"], capture_output=True, text=True)
        if blob.returncode != 0:
            continue
        try:
            ops = json.loads(blob.stdout)["ops"]
        except (ValueError, KeyError):
            kv_set(conn, f"inbox:{ref}", "bad-json")
            continue
        res = apply_ops(conn, ops, now)
        kv_set(conn, f"inbox:{ref}", json.dumps(res)[:400])
        applied += 1
        with open(done, "a", encoding="utf-8") as f:
            f.write(ref.split("origin/", 1)[1] + "\n")
    return applied
