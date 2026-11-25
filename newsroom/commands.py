"""Telegram command handler. Only the configured chat may issue commands. Polled once per cycle (up to ~15 min latency)."""
from __future__ import annotations

import json

from . import editorial as ed
from .db import kv_get, kv_set
from .notify import e
from .util import iso
from .verify import can_approve, outstanding, refresh_status, set_claim

HELP = ("Commands:\n/status\n/top\n/story ID\n/verify ID CLAIM_ID STATUS [note]  (STATUS: VERIFIED, COMPANY-REPORTED, "
        "INDEPENDENTLY CORROBORATED, REPORTED ALLEGATION, DISPUTED, UNVERIFIED)\n/approve ID [kind] [destination]  (approves the LATEST version; any later edit voids it)\n"
        "/reject ID [reason]\n/research ID note\n/published ID\n/correct ID text\n/lead title | significance\n/breaking ID [hours]\n/unbreak ID\n"
        "/live ID\n/livestate ID LIVE|PAUSED|CONCLUDED\n/liveadd ID text | source url\n/liveapprove UPDATE_ID\n"
        "/confirm_offline  (after switching your computer off and seeing a cloud run)")


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
            return f"Story #{sid} marked published." if r["ok"] else f"Cannot mark published: {r['error']}."
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
    res = notifier._call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]}, retries=1)
    n = 0
    for u in res.get("result", []) if res.get("ok") else []:
        kv_set(conn, "tg_offset", u["update_id"] + 1)
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(notifier.chat_id):
            continue  # ignore everyone but the owner
        text = (m.get("text") or "").strip()
        if text.startswith("/"):
            notifier.send_raw(e(handle(conn, text, now)), now)
            n += 1
    return n
