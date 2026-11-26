"""One-tap review: plain checklist, Telegram buttons, actions and 10-minute Undo.

Every story card shows five yes/no checks: Sources found, Facts checked, Dates confirmed, Second check passed, Links verified.
Approve is refused until all five are yes, and the card says in one plain line what is missing.
Nothing here publishes to Shopify. Approve only marks the story approved in the newsroom; the approved-only export then feeds the website.

Button data is "pm:<action>:<id>" (Telegram allows 64 bytes). Only the configured chat can press buttons (checked in commands.poll)."""
from __future__ import annotations

import json
from datetime import timedelta

from . import cards, editorial as ed, linkcheck, secondcheck
from .db import kv_get, kv_set
from .notify import e
from .util import iso, parse_iso
from .verify import can_approve, open_sources, outstanding, refresh_status

UNDO_MINUTES = 10
TEST_CATEGORY = "TEST CARD"
SAMPLE_CATEGORY = "SAMPLE"
CHECKS = (("sources", "Sources found"), ("facts", "Facts checked"), ("dates", "Dates confirmed"),
          ("second", "Second check passed"), ("links", "Links verified"))


# ---------------------------------------------------------------- checklist
def _story(conn, sid):
    return conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()


def is_test(s) -> bool:
    return bool(s) and ((s["category"] or "") == TEST_CATEGORY or bool(s["test_only"]))


def checklist(conn, sid: int) -> dict:
    s = _story(conn, sid)
    if s and (s["category"] or "") == TEST_CATEGORY:      # the Telegram test card: a safe, scripted example (see apply_action)
        links = kv_get(conn, "testcard:links", "0") == "1"
        flags = {"sources": True, "facts": True, "dates": True, "second": True, "links": links}
        why = {} if links else {"links": "one link has not been re-opened yet"}
    else:
        ok_facts, out = can_approve(conn, sid)
        n_unsourced = len(linkcheck.unsourced(conn, sid))
        lok, lwhy = linkcheck.gate(conn, sid)
        sok, swhy = secondcheck.gate(conn, sid)
        flags = {"sources": bool(s["evidence_ok"]) and n_unsourced == 0, "facts": ok_facts, "dates": bool(s["pub_known"]),
                 "second": sok, "links": lok}
        why = {}
        if not flags["sources"]:
            why["sources"] = (f"{n_unsourced} fact(s) have no source link" if n_unsourced else "no official or second outlet source found yet")
        if not flags["facts"]:
            why["facts"] = (f"{len(out)} fact(s) not checked yet" if out else "no facts recorded yet")
        if not flags["dates"]:
            why["dates"] = "the source did not give a publish date"
        if not flags["second"]:
            why["second"] = "the second check has not passed" if "not run" not in swhy else "the second check has not been run"
        if not flags["links"]:
            why["links"] = lwhy
    miss = [why[k] for k, _ in CHECKS if k in why]
    return {"flags": flags, "why": why, "all_ok": all(flags.values()), "missing": miss}


def checklist_text(cl: dict) -> str:
    lines = ["<b>Checklist</b>"]
    for k, label in CHECKS:
        lines.append(f"{'✅' if cl['flags'][k] else '❌'} {label}: {'yes' if cl['flags'][k] else 'no'}")
    if cl["all_ok"]:
        lines.append("All five are yes. Safe to approve.")
    else:
        lines.append("Missing: " + "; ".join(cl["missing"]) + ". Tap “Needs more checking” and I will run the checks.")
    return "\n".join(lines)


# ---------------------------------------------------------------- card + buttons
def card_text(conn, sid: int, now, header: str = "") -> str:
    s = _story(conn, sid)
    c = cards.card(conn, s, now)
    cl = checklist(conn, sid)
    body = [f"{header}📰 <b>{e(c['uid'])}</b> · score {c['score']} · {e(c['category'])}", f"<b>{e(c['headline'])}</b>",
            f"{e(c['age'])} · {e(c['source'])}"]
    if c["link"]:
        body.append(e(c["link"]))
    body += ["", checklist_text(cl)]
    return "\n".join(body)


def _b(text: str, action: str, sid) -> dict:
    return {"text": text, "callback_data": f"pm:{action}:{sid}"}


def keyboard(conn, sid: int) -> dict:
    s = _story(conn, sid)
    done = s["editorial_status"] in ("approved", "published")
    return {"inline_keyboard": [
        [_b("✔ Approved" if done else "✅ Approve", "noop" if done else "approve", sid), _b("🔍 Needs more checking", "more", sid)],
        [_b("❌ Reject", "reject", sid)],
        [_b("🚨 Mark BREAKING", "breaking", sid), _b("🔴 Start LIVE", "live", sid)]]}


def undo_keyboard(token: int) -> dict:
    return {"inline_keyboard": [[{"text": f"↩ Undo ({UNDO_MINUTES} min)", "callback_data": f"pm:undo:{token}"}]]}


def briefing_keyboard(conn, ids: list[int]) -> dict | None:
    if not ids:
        return None
    return {"inline_keyboard": [[_b(f"Review {i + 1}", "card", sid) for i, sid in enumerate(ids)]]}


def send_card(conn, notifier, sid: int, now, header: str = "") -> str:
    return notifier.send_once(f"card:{sid}:{iso(now)}", "card", card_text(conn, sid, now, header), now, story_id=sid, markup=keyboard(conn, sid))


# ---------------------------------------------------------------- automatic checks and draft
def auto_checks(conn, sid: int, now, data_dir: str = "data", cfg=None, get=None, wb=None, light: bool = False) -> None:
    """Run everything that can be checked without a human: open sources, second check, link check. Failures are recorded, never hidden."""
    from .fetch import http_get
    get = get or http_get
    if light and wb is None and get is http_get:      # alerts: only look up existing captures; saving new ones is done when you tap Approve
        from . import evidence
        wb = lambda u: evidence.wayback(u, allow_save=False)  # noqa: E731
    try:
        if not conn.execute("SELECT 1 FROM source_checks WHERE story_id=?", (sid,)).fetchone():
            open_sources(conn, sid, now, get=get)
        refresh_status(conn, sid)
        sm = secondcheck.summary(conn, sid)
        if not sm["ran"] or sm["overall"] != "PASS":
            secondcheck.run(conn, sid, now, get=get)
        linkcheck.run(conn, sid, now, data_dir, cfg, get=get, wb=wb)
    except Exception as ex:  # noqa: BLE001
        conn.execute("INSERT INTO runs(bot,started_at,finished_at,status,detail) VALUES('review',?,?,'error',?)", (iso(now), iso(now), f"{type(ex).__name__}: {ex}"[:300]))
        conn.commit()


def ensure_draft(conn, sid: int, now) -> None:
    """Approval needs saved content. If none exists, save a plain one built ONLY from stored facts (headline, the feed's own excerpt, the source)."""
    if ed.latest_versions(conn, sid):
        return
    s = _story(conn, sid)
    sm = cards.summary_of(conn, sid, s["headline"])
    org = s["source_org"] or "the source"
    body = f"{s['headline']}\n\n{sm}\n\nReported by {org}" + (f" on {s['published_at'][:10]}." if s["published_at"] else ".")
    ed.save_version(conn, sid, "article", body, now, author="auto-from-source-record")


# ---------------------------------------------------------------- actions
def _undo_token(conn, now, action: str, sid, data: dict) -> int:
    n = int(kv_get(conn, "undo_seq", "0")) + 1
    kv_set(conn, "undo_seq", n)
    kv_set(conn, f"undo:{n}", json.dumps({"action": action, "sid": sid, "at": iso(now), "data": data}))
    return n


def _result(text: str, markup=None, dirty=False, new=False, answer="") -> dict:
    return {"text": text, "markup": markup, "site_dirty": dirty, "new_message": new, "answer": answer}


def apply_action(conn, action: str, ident: int, now, data_dir: str = "data", cfg=None, get=None, wb=None) -> dict:
    """Perform one button action. Returns {text, markup, site_dirty, new_message, answer}."""
    if action == "noop":
        return _result("", answer="Already approved. Use Undo from the confirmation if you need to take it back.")
    if action == "undo":
        return _undo(conn, ident, now)
    if action in ("lvapprove", "lvskip"):
        return _live_update(conn, action, ident, now)
    s = _story(conn, ident)
    if not s:
        return _result("That story no longer exists.", answer="Not found")
    test = is_test(s)
    uid, head = ed.uid(ident), e(s["headline"])

    if action == "card":
        return _result(card_text(conn, ident, now), keyboard(conn, ident), new=True, answer="")

    if action == "more" and (s["category"] or "") == SAMPLE_CATEGORY:
        return _result(card_text(conn, ident, now, "🔍 SAMPLE card: checks are not run on sample stories.\n"), keyboard(conn, ident), answer="Sample: nothing to check")
    if action == "more":
        if test:
            kv_set(conn, "testcard:links", "1")
        else:
            ed.request_research(conn, ident, now, "owner tapped: needs more checking")
            auto_checks(conn, ident, now, data_dir, cfg, get, wb)
        return _result(card_text(conn, ident, now, "🔍 Checks re-run.\n"), keyboard(conn, ident), answer="Checks re-run")

    if action == "approve" and (s["category"] or "") == SAMPLE_CATEGORY:
        return _result(card_text(conn, ident, now, "⛔ <b>SAMPLE card:</b> it can never be approved or published. The buttons only show how review works.\n"), keyboard(conn, ident),
                       answer="Sample: never published")
    if action == "more" and (s["category"] or "") == SAMPLE_CATEGORY:
        return _result(card_text(conn, ident, now, "🔍 SAMPLE card: checks are not run on sample stories.\n"), keyboard(conn, ident), answer="Sample: nothing to check")
    if action == "approve":
        if not test:
            auto_checks(conn, ident, now, data_dir, cfg, get, wb)          # a fresh link check always runs before approval
        cl = checklist(conn, ident)
        if not cl["all_ok"]:
            return _result(card_text(conn, ident, now, "⛔ Cannot approve yet.\n"), keyboard(conn, ident), answer="Not yet: " + cl["missing"][0][:120])
        if test:
            kv_set(conn, "testcard:approved", "1")
            tok = _undo_token(conn, now, "approve", ident, {"test": True})
            return _result(f"✅ <b>TEST: approved</b>\n{head}\nOn a real story this would now appear on the website within about 5 minutes.\nThis is only a test card: nothing was published.",
                           undo_keyboard(tok), answer="Test approved")
        ensure_draft(conn, ident, now)
        before = conn.execute("SELECT COALESCE(MAX(id),0) m FROM approvals").fetchone()["m"]
        r = ed.approve(conn, ident, now, None, "all")
        if not r["ok"]:
            return _result(card_text(conn, ident, now, f"⛔ Cannot approve: {e(r['error'])}.\n"), keyboard(conn, ident), answer="Blocked")
        ids = [x["id"] for x in conn.execute("SELECT id FROM approvals WHERE id>? AND story_id=? AND action='approve' AND valid=1", (before, ident))]
        sh = _shopify_on_approve(conn, ident)
        tok = _undo_token(conn, now, "approve", ident, {"approval_ids": ids, "shopify": sh.get("prev")})
        return _result(f"✅ <b>Approved</b> · {uid}\n{head}\nIt goes to the website feed within about 5 minutes, with its sources and archived copies.\n{sh['note']}\nYou can undo this for {UNDO_MINUTES} minutes.",
                       undo_keyboard(tok), dirty=True, answer="Approved")

    if action == "reject":
        prev = s["editorial_status"]
        if test:
            kv_set(conn, "testcard:approved", "0")
        else:
            ed.reject(conn, ident, now, "owner tapped Reject")
        tok = _undo_token(conn, now, "reject", ident, {"prev": prev, "test": test})
        return _result(f"❌ <b>Rejected</b> · {uid}\n{head}\nIt will not be published.\nYou can undo this for {UNDO_MINUTES} minutes.", undo_keyboard(tok), dirty=True, answer="Rejected")

    if action == "breaking":
        prev = {"b": s["breaking"], "u": s["breaking_until"]}
        if not test:
            ed.set_breaking(conn, ident, now, 6)
        tok = _undo_token(conn, now, "breaking", ident, dict(prev, test=test))
        approved = ed.can_publish(conn, ident, "website")[0] if not test else True
        note = "The red banner shows on the website for up to 6 hours." if approved else "The banner will show on the website once you approve this story."
        return _result(f"🚨 <b>Marked BREAKING</b> · {uid}\n{head}\n{note} It switches off by itself.\nYou can undo this for {UNDO_MINUTES} minutes.",
                       undo_keyboard(tok), dirty=True, answer="Marked breaking")

    if action == "live":
        existed = bool(conn.execute("SELECT 1 FROM live_coverage WHERE story_id=?", (ident,)).fetchone())
        if not test:
            ed.live_open(conn, ident, now)
        tok = _undo_token(conn, now, "live", ident, {"existed": existed, "test": test})
        return _result(f"🔴 <b>LIVE coverage started</b> · {uid}\n{head}\nNew items arrive here as draft updates. Tap Approve on each one; only approved updates appear on the website.\nYou can undo this for {UNDO_MINUTES} minutes.",
                       undo_keyboard(tok), dirty=True, answer="Live started")
    return _result("Unknown button.", answer="Unknown")


def _shopify_on_approve(conn, sid: int) -> dict:
    """After a real Approve tap: make the matching Shopify article Visible with the pm-approved tag. Never blocks the approval."""
    try:
        from . import shopify
        r = shopify.approve_sync(conn, sid)
    except Exception as ex:  # noqa: BLE001
        return {"prev": None, "note": f"Shopify article not changed ({type(ex).__name__})."}
    if r.get("changed"):
        return {"prev": r["prev"], "note": "The matching Shopify article is now Visible with the pm-approved tag."}
    return {"prev": None, "note": "Shopify article not changed: " + r.get("why", "unknown") + "."}


def _undo_shopify(d: dict) -> str:
    if not d.get("shopify"):
        return ""
    try:
        from . import shopify
        r = shopify.undo_sync(d["shopify"])
    except Exception as ex:  # noqa: BLE001
        return f" Shopify article NOT restored ({type(ex).__name__}); check it in Shopify."
    return " The Shopify article is back to how it was." if r.get("changed") else " Shopify article not restored: " + r.get("why", "unknown") + "."


def _undo(conn, token: int, now) -> dict:
    raw = kv_get(conn, f"undo:{token}")
    if not raw:
        return _result("", answer="Nothing to undo")
    u = json.loads(raw)
    if u.get("done"):
        return _result("↩ Already undone.", answer="Already undone")
    if now - parse_iso(u["at"]) > timedelta(minutes=UNDO_MINUTES):
        return _result(f"⏱ Too late to undo: more than {UNDO_MINUTES} minutes have passed. Use the card to change the story.", answer="Too late")
    sid, d, act = u["sid"], u["data"], u["action"]
    shop_note = ""
    if act == "lvapprove":
        conn.execute("UPDATE live_updates SET status='draft', approved_at=NULL, approved_hash=NULL WHERE id=?", (sid,))
        conn.commit()
    elif d.get("test"):
        kv_set(conn, "testcard:approved", "0")
    elif act == "approve":
        for aid in d.get("approval_ids", []):
            conn.execute("UPDATE approvals SET valid=0, invalidated_at=?, invalidated_reason='undone by owner' WHERE id=?", (iso(now), aid))
        conn.execute("INSERT INTO approvals(story_id,kind,action,reason,at,valid) VALUES(?,?,?,?,?,0)", (sid, "story", "undo", "owner undid the approval", iso(now)))
        ed._sync_story_status(conn, sid)
        conn.commit()
        shop_note = _undo_shopify(d)
    elif act == "reject":
        conn.execute("UPDATE stories SET editorial_status=? WHERE id=?", (d.get("prev") or "verifying", sid))
        conn.execute("INSERT INTO approvals(story_id,kind,action,reason,at,valid) VALUES(?,?,?,?,?,0)", (sid, "story", "reject_undone", "owner undid the rejection", iso(now)))
        conn.commit()
    elif act == "breaking":
        conn.execute("UPDATE stories SET breaking=?, breaking_until=? WHERE id=?", (d.get("b") or 0, d.get("u"), sid))
        conn.commit()
    elif act == "live":
        if not d.get("existed"):
            ed.live_state(conn, sid, "CONCLUDED", now)
    u["done"] = True
    kv_set(conn, f"undo:{token}", json.dumps(u))
    label = "update" if act == "lvapprove" else ed.uid(sid)
    return _result(f"↩ <b>Undone</b> ({act}) · {label}. Nothing is left changed.{shop_note}", None, dirty=True, answer="Undone")


# ---------------------------------------------------------------- live updates
def live_update_card(conn, upd_id: int) -> tuple[str, dict]:
    u = conn.execute("SELECT * FROM live_updates WHERE id=?", (upd_id,)).fetchone()
    cov = conn.execute("SELECT title FROM live_coverage WHERE id=?", (u["coverage_id"],)).fetchone()
    text = (f"🔴 <b>LIVE update to approve</b> · {ed.uid(u['story_id'])}\n{e(cov['title'] if cov else '')}\n\n{e(u['text'])}\n"
            f"Source: {e(u['source_url'] or 'none, cannot be approved')}\nLabel: {e(u['classification'])}")
    kb = {"inline_keyboard": [[_b("✅ Approve update", "lvapprove", upd_id), _b("Skip", "lvskip", upd_id)]]}
    return text, kb


def _live_update(conn, action: str, upd_id: int, now) -> dict:
    u = conn.execute("SELECT * FROM live_updates WHERE id=?", (upd_id,)).fetchone()
    if not u:
        return _result("That update no longer exists.", answer="Not found")
    if action == "lvskip":
        conn.execute("UPDATE live_updates SET status='skipped' WHERE id=?", (upd_id,))
        conn.commit()
        return _result(f"Skipped. It will not be published.\n{e(u['text'])[:200]}", answer="Skipped")
    r = ed.live_approve_update(conn, upd_id, now)
    if not r["ok"]:
        return _result(f"⛔ Cannot approve this update: {e(r['error'])}.", answer="Blocked")
    tok = _undo_token(conn, now, "lvapprove", upd_id, {})
    return _result(f"✅ <b>Update approved</b> · it appears on the Live page within about 5 minutes.\n{e(u['text'])[:300]}\nYou can undo this for {UNDO_MINUTES} minutes.", undo_keyboard(tok), dirty=True, answer="Approved")


def send_live_update_cards(conn, notifier, now, limit: int = 5) -> int:
    n = 0
    for u in conn.execute("SELECT u.id FROM live_updates u JOIN live_coverage c ON c.id=u.coverage_id WHERE u.status='draft' AND c.state='LIVE' ORDER BY u.id"):
        if n >= limit:
            break
        text, kb = live_update_card(conn, u["id"])
        if notifier.send_once(f"liveupd:{u['id']}", "liveupd", text, now, markup=kb) == "sent":
            n += 1
    return n


# ---------------------------------------------------------------- Telegram test card
def ensure_test_card(conn, now) -> int:
    """A clearly-labelled sample story (lead_only, so it can never reach the briefing, dashboard top 5 or the website export)."""
    r = conn.execute("SELECT id FROM stories WHERE category=?", (TEST_CATEGORY,)).fetchone()
    if r:
        conn.execute("UPDATE stories SET test_only=1, lead_only=1 WHERE id=?", (r["id"],))
        return r["id"]
    cur = conn.execute("INSERT INTO stories(headline,headline_norm,source_org,detected_at,last_item_at,category,score,lead_only,pub_known,evidence_ok,editorial_status,test_only) "
                       "VALUES(?,?,?,?,?,?,?,1,1,1,'verifying',1)",
                       ("TEST CARD: tap the buttons to see how review works. Nothing here is real.", "test card", "Post-Mortem test", iso(now), iso(now), TEST_CATEGORY, 0))
    conn.commit()
    return cur.lastrowid


def send_test_card(conn, notifier, now) -> str:
    sid = ensure_test_card(conn, now)
    kv_set(conn, "testcard:links", "0")
    kv_set(conn, "testcard:approved", "0")
    text = ("🧪 <b>TEST CARD</b>: try the buttons. Nothing here is real and nothing is published.\n"
            "1) Tap Approve: it is refused and says what is missing.\n2) Tap “Needs more checking”: the missing check turns to yes.\n"
            "3) Tap Approve again, then Undo.\n\n" + card_text(conn, sid, now))
    return notifier.send_once(f"testcard:{iso(now)}", "testcard", text, now, story_id=sid, markup=keyboard(conn, sid))


# ---------------------------------------------------------------- the DOJ SAMPLE story (never publishable)
SAMPLE_HEAD = "SAMPLE: Former CEO indicted for $1.2 billion securities fraud and money laundering, DOJ documents unsealed"
SAMPLE_URL = "https://doj.gov/p1"


def ensure_sample_card(conn, now) -> int:
    """A made-up story shaped like a real one. test_only + lead_only: the exporter, briefing and Approve all refuse it."""
    r = conn.execute("SELECT id FROM stories WHERE category=?", (SAMPLE_CATEGORY,)).fetchone()
    if r:
        conn.execute("UPDATE stories SET test_only=1, lead_only=1 WHERE id=?", (r["id"],))
        return r["id"]
    pub = iso(now - timedelta(minutes=30))
    cur = conn.execute("INSERT INTO stories(headline,headline_norm,source_org,detected_at,last_item_at,published_at,category,score,lead_only,pub_known,evidence_ok,editorial_status,test_only) "
                       "VALUES(?,?,?,?,?,?,?,?,1,1,1,'verifying',1)", (SAMPLE_HEAD, "sample doj indicted", "US Department of Justice (SAMPLE)", iso(now), iso(now), pub, SAMPLE_CATEGORY, 0))
    sid = cur.lastrowid
    conn.execute("INSERT INTO items(story_id,source_id,org,bot,tier,title,url,canonical_url,content_hash,published_at,pub_known,detected_at,summary,tags) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                 (sid, "sample", "US Department of Justice (SAMPLE)", "primary", "primary", SAMPLE_HEAD, SAMPLE_URL, "sample:" + SAMPLE_URL, "sample", pub, iso(now),
                  "SAMPLE TEXT. A made-up press release used only to show how a review card looks. Nothing in it is real.", "official-source,sample"))
    for typ, text, status in (("core", "The Department of Justice says a former chief executive was indicted on securities fraud and money laundering charges.", "COMPANY-REPORTED"),
                              ("figure", "The indictment refers to $1.2 billion.", "VERIFIED"),
                              ("dates", "The documents were unsealed on 24 November 2026.", "UNVERIFIED"),
                              ("names", "The names of the people and companies are correct.", "UNVERIFIED")):
        conn.execute("INSERT INTO claims(story_id,claim_type,claim_text,status,source_url) VALUES(?,?,?,?,?)", (sid, typ, text, status, SAMPLE_URL))
    conn.commit()
    return sid


def send_sample_card(conn, notifier, now) -> str:
    sid = ensure_sample_card(conn, now)
    it = conn.execute("SELECT * FROM items WHERE story_id=? LIMIT 1", (sid,)).fetchone()
    src = "\n".join([f"1. {e(it['org'])} (official source): {e(SAMPLE_URL)}",
                       f"   published {(it['published_at'] or '')[:10]} - retrieved: not yet - archived copy: not yet"])
    text = "\n\n".join(["🧪 <b>SAMPLE: not a real story. It can never be published.</b>\nTry the buttons to see how review works.",
                          card_text(conn, sid, now), "<b>Summary</b>\n" + e(it["summary"]), "<b>Sources</b>\n" + src])
    return notifier.send_once(f"samplecard:{iso(now)}", "samplecard", text, now, story_id=sid, markup=keyboard(conn, sid))
