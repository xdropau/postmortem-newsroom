"""Telegram command handler. Only the configured chat may issue commands. Polled once per cycle (up to ~15 min latency)."""
from __future__ import annotations

import json

from . import changelog, editorial as ed, evidence, linkcheck, review, secondcheck
import os
import sys
from .db import kv_get, kv_set
from .notify import e
from .util import iso
from .verify import can_approve, outstanding, refresh_status, set_claim

HELP = ("Commands:\n/status\n/top\n/story ID\n/verify ID CLAIM_ID STATUS [note]  (STATUS: VERIFIED, COMPANY-REPORTED, "
        "INDEPENDENTLY CORROBORATED, REPORTED ALLEGATION, DISPUTED, UNVERIFIED)\n/approve ID [kind] [destination]  (approves the LATEST version; any later edit voids it)\n"
        "/reject ID [reason]\n/research ID note\n/published ID\n/correct ID text\n/lead title | significance\n/breaking ID [hours]\n/unbreak ID\n"
        "/live ID\n/livestate ID LIVE|PAUSED|CONCLUDED\n/liveadd ID text | source url\n/liveapprove UPDATE_ID\n"
        "/links ID  (re-open every cited link now)\n/linkok ID CLAIM_ID note  (you opened a link yourself that robots cannot check)\n"
        "/confirm_offline  (after switching your computer off and seeing a cloud run)\n\nTip: tap the buttons on a story card instead of typing.")


def handle(conn, text: str, now) -> str:
    parts = text.strip().split()
    if not parts:
        return HELP
    cmd = parts[0].split("@")[0].lower()
    try:
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/confirm_offline":
            kv_set(conn, "owner_confirmed_offline", iso(now))
            return "Recorded: cloud execution confirmed while your computer was off."
        if cmd == "/status":
            rows = conn.execute("SELECT editorial_status s, COUNT(*) c FROM stories WHERE lead_only=0 GROUP BY 1").fetchall()
            runs = conn.execute("SELECT bot, status, finished_at FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY bot)").fetchall()
            return "Stories: " + (", ".join(f"{r['s']} {r['c']}" for r in rows) or "none") + "\nLast runs:\n" + \
                "\n".join(f"{r['bot']}: {r['status']} {r['finished_at']}" for r in runs)
        if cmd == "/top":
            rows = conn.execute("SELECT id, score, headline, verification_status FROM stories WHERE lead_only=0 AND last_item_at>=? "
                                "ORDER BY score DESC LIMIT 5", (iso(now.replace(hour=0, minute=0, second=0)),)).fetchall()
            return "\n".join(f"#{r['id']} [{r['score']}] {r['headline'][:90]} ({r['verification_status']})" for r in rows) or "No stories today."
        if cmd == "/lead":
            title, _, why = " ".join(parts[1:]).partition("|")
            r = ed.add_lead(conn, now, title, significance=why.strip())
            return f"Lead #{r['id']} recorded (status new, originality NOT CHECKED)." if r["ok"] else r["error"]
        sid = int(parts[1])
        s = conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
        if not s and cmd != "/liveapprove":
            return f"No story #{sid}."
        if cmd == "/story":
            out = outstanding(conn, sid)
            vers = ", ".join(f"{v['kind']} v{v['version']}{' ✔approved' if ed._is_approved(conn, v) else ' (not approved)'}" for v in ed.latest_versions(conn, sid)) or "none"
            return (f"#{sid} {ed.uid(sid)} [{s['score']}] {s['headline']}\nStatus: {s['verification_status']} | editorial {s['editorial_status']} | "
                    f"publication {s['publication_status']}\nPackage: {s['package_path'] or 'none'}\nOutstanding:\n" +
                    ("\n".join(f"  #{o['id']} {o['status']}: {o['claim_text'][:100]}" for o in out) or "  none"))
        if cmd == "/verify":
            cid, status, note = int(parts[2]), parts[3], " ".join(parts[4:])
            # allow multiword statuses: join until a number-free remainder is unknown; keep simple with underscores/hyphens
            if not set_claim(conn, sid, cid, status, note, now):
                return "Could not set claim. Use a valid STATUS (spaces as underscores, e.g. INDEPENDENTLY_CORROBORATED)."
            ok, out = can_approve(conn, sid)
            if ok and s["editorial_status"] in ("new", "verifying"):
                conn.execute("UPDATE stories SET editorial_status='ready' WHERE id=?", (sid,))
                conn.commit()
            return f"Claim #{cid} set. Story status {refresh_status(conn, sid)}. " + ("All claims resolved: READY for approval." if ok else f"{len(out)} UNVERIFIED remain.")
        if cmd == "/secondcheck":
            secondcheck.run(conn, sid, now)
            secondcheck.write_part_e(conn, sid, os.environ.get("DATA_DIR", "data"))
            return secondcheck.telegram_text(conn, sid)
        if cmd == "/links":
            from .config import load_all
            res = linkcheck.run(conn, sid, now, os.environ.get("DATA_DIR", "data"), load_all())
            bad = [r for r in res["rows"] if not r["ok"]]
            return (f"Links for {ed.uid(sid)}: " + ("all verified." if res["ok"] else f"{len(bad)} problem(s):\n") +
                    "\n".join(f"claim #{r['claim_id']} {r['url'][:70] or '(no link)'}: {'; '.join(r['reasons'])}" for r in bad[:8]))
        if cmd == "/linkok":
            if linkcheck.resolve(conn, sid, int(parts[2]), " ".join(parts[3:]), now):
                return f"Recorded: you verified the link for claim #{parts[2]}. Re-run /links {sid}."
            return "Could not record. Use: /linkok ID CLAIM_ID note (a note of 3+ characters is required)."
        if cmd == "/scresolve":     # /scresolve ID KEY PASS|FAIL|UNCLEAR note...
            if secondcheck.resolve(conn, sid, parts[2], parts[3], " ".join(parts[4:]), now):
                secondcheck.write_part_e(conn, sid, os.environ.get("DATA_DIR", "data"))
                return secondcheck.telegram_text(conn, sid)
            return "Could not resolve. Use: /scresolve ID KEY PASS|FAIL|UNCLEAR note (a note of 3+ characters is required). Keys: " + ", ".join(secondcheck.MATERIAL + secondcheck.ADVISORY)
        if cmd == "/evidence":
            from .config import load_all
            dd = os.environ.get("DATA_DIR", "data")
            rows = evidence.archive_story(conn, sid, now, dd, load_all())
            rel = evidence.bundle(conn, sid, now, dd)
            ok = sum(1 for r in rows if r["http_status"] == 200)
            wb = sum(1 for r in rows if r["wayback_url"])
            full = sum(1 for r in rows if r["full_copy_path"])
            return (f"Evidence bundle for {ed.uid(sid)} written to the PRIVATE data repo: {rel}\n"
                    f"{ok}/{len(rows)} sources retrieved, {full} official full copies, {wb} archive.org captures. "
                    f"Open it in github.com/<you>/postmortem-newsroom-data (private).")
        if cmd == "/approve":
            kind = parts[2] if len(parts) > 2 and parts[2] in ed.KINDS else None
            dest = parts[3] if len(parts) > 3 else "all"
            r = ed.approve(conn, sid, now, kind, dest)
            if not r["ok"]:
                out = r.get("outstanding") or []
                return f"Cannot approve: {r['error']}.\n" + "\n".join(f"#{o['id']} {o['claim_text'][:100]}" for o in out[:8])
            return f"Story #{sid} ({ed.uid(sid)}): approved {', '.join(r['approved'])} for {dest}. Story is now {r['status']}. Nothing has been published; publishing remains manual."
        if cmd == "/reject":
            ed.reject(conn, sid, now, " ".join(parts[2:]))
            return f"Story #{sid} rejected (reason recorded)."
        if cmd == "/research":
            ed.request_research(conn, sid, now, " ".join(parts[2:]))
            return f"Research requested for #{sid}; moved back to verifying."
        if cmd == "/published":
            r = ed.mark_published(conn, sid, now)
            if not r["ok"]:
                return f"Cannot mark published: {r['error']}."
            from .config import load_all
            rel = evidence.permanent_bundle(conn, sid, now, os.environ.get("DATA_DIR", "data"), load_all())
            return f"Story #{sid} marked published. Permanent evidence bundle: {rel} (private repo). Tie it to the web page with /articleurl {sid} URL."
        if cmd == "/articleurl":
            r = ed.link_story(conn, sid, "article_url", parts[2], now)
            if not r["ok"]:
                return r["error"]
            from .config import load_all
            rel = evidence.permanent_bundle(conn, sid, now, os.environ.get("DATA_DIR", "data"), load_all())
            return f"{ed.uid(sid)} linked to {parts[2]}. Permanent bundle updated: {rel}"
        if cmd == "/correct":
            r = ed.add_correction(conn, sid, " ".join(parts[2:]), now)
            return f"Correction #{r['id']} logged for {ed.uid(sid)}." if r["ok"] else r["error"]
        if cmd == "/breaking":
            r = ed.set_breaking(conn, sid, now, float(parts[2]) if len(parts) > 2 else 6)
            return f"#{sid} flagged BREAKING until {r['until']} (auto-expires; max {ed.MAX_BREAKING_HOURS}h)."
        if cmd == "/unbreak":
            ed.clear_breaking(conn, sid)
            return f"#{sid} breaking flag cleared."
        if cmd == "/live":
            r = ed.live_open(conn, sid, now)
            return f"Live coverage {'already exists' if r.get('existing') else 'opened'} for #{sid} (state LIVE). New items become DRAFT updates; none is public until you /liveapprove."
        if cmd == "/livestate":
            return "State set." if ed.live_state(conn, sid, parts[2], now) else "Use LIVE, PAUSED or CONCLUDED on a story with live coverage."
        if cmd == "/liveadd":
            body = " ".join(parts[2:])
            txt, _, url = body.partition("|")
            r = ed.live_add_update(conn, sid, txt, now, url.strip())
            return f"Draft update #{r['id']} saved (not public)." if r.get("ok") else r["error"]
        if cmd == "/liveapprove":
            r = ed.live_approve_update(conn, sid, now)   # here "sid" is the update id
            return "Update approved for public display." if r["ok"] else r["error"]
    except (IndexError, ValueError):
        return "Bad command format.\n" + HELP
    return HELP


def poll(conn, notifier, now) -> int:
    if not notifier.configured:
        return 0
    offset = int(kv_get(conn, "tg_offset", "0"))
    res = notifier._call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message", "callback_query"]}, retries=1)
    n = 0
    if not res.get("ok"):                         # say so in the log instead of silently handling nothing
        print(f"getUpdates failed: {res.get('status') or res.get('error') or res.get('description')}", file=sys.stderr)
    elif not res.get("result"):
        print("getUpdates: no pending taps or messages")
    for u in res.get("result", []) if res.get("ok") else []:
        kv_set(conn, "tg_offset", u["update_id"] + 1)
        cq = u.get("callback_query")
        if cq:                                   # a button tap
            m = cq.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != str(notifier.chat_id):
                continue                         # ignore everyone but the owner
            n += _tap(conn, notifier, cq, m, now)
            continue
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(notifier.chat_id):
            continue  # ignore everyone but the owner
        text = (m.get("text") or "").strip()
        if text.startswith("/"):
            parts = text.split()
            if parts[0].split("@")[0].lower() == "/story" and len(parts) > 1 and parts[1].isdigit() and conn.execute("SELECT 1 FROM stories WHERE id=?", (int(parts[1]),)).fetchone():
                notifier.send_raw(review.card_text(conn, int(parts[1]), now), now, review.keyboard(conn, int(parts[1])))
            else:
                notifier.send_raw(e(handle(conn, text, now)), now)
            changelog.seal(conn)
            n += 1
    return n


def _tap(conn, notifier, cq, m, now) -> int:
    parts = (cq.get("data") or "").split(":")
    if len(parts) != 3 or parts[0] != "pm" or not parts[2].lstrip("-").isdigit():
        notifier.answer(cq["id"], "Unknown button")
        return 0
    try:
        r = review.apply_action(conn, parts[1], int(parts[2]), now, data_dir=os.environ.get("DATA_DIR", "data"))
    except Exception as ex:  # noqa: BLE001 - a failed tap must never stop the poll
        conn.execute("INSERT INTO runs(bot,started_at,finished_at,status,detail) VALUES('review',?,?,'error',?)", (iso(now), iso(now), f"{type(ex).__name__}: {ex}"[:300]))
        conn.commit()
        notifier.answer(cq["id"], "Something went wrong; nothing was changed")
        notifier.send_raw("⚠️ That button failed and nothing was changed. Try again, or use the typed command.", now)
        return 0
    notifier.answer(cq["id"], r["answer"])
    if r["text"]:
        if r["new_message"] or not m.get("message_id"):
            notifier.send_raw(r["text"], now, r["markup"])
        else:
            notifier.edit(m["message_id"], r["text"], r["markup"], now)
            # the card was edited in place, which is easy to miss (or looks unchanged): always add a plain result message too
            notifier.send_raw(f"{'⛔' if r['answer'].startswith(('Not yet', 'Blocked')) else '✅'} Result: {e(r['answer'] or 'done')}", now)
    else:
        notifier.send_raw(f"✅ Result: {e(r['answer'] or 'done')}", now)
    changelog.seal(conn)
    return 1
