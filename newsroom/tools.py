"""Audit, live self-test and acceptance checks. Acceptance reads real recorded evidence; it cannot be talked into passing."""
from __future__ import annotations

import json
import os
from datetime import timedelta

from .briefing import build as build_briefing
from .config import ROOT
from .cycle import mark_source, record_run, send_alerts
from .db import kv_get, kv_set
from .fetch import FetchError, http_get, parse_feed
from .ingest import ingest, rescore_recent
from .notify import e
from . import editorial as ed
from .package import build_package
from .util import human_age, iso, parse_iso
from .verify import seed_claims, open_sources


def audit(conn, cfg, notifier, now, get=http_get) -> list[dict]:
    """Fetch every enabled feed once (no ingest) and report what really works."""
    rows = []
    for src in [s for s in cfg["sources"]["sources"] if s.get("enabled", True)]:
        row = {"id": src["id"], "org": src["org"], "bot": src["bot"], "status": "OK", "entries": 0, "newest_age": None, "no_timestamp": 0, "link_ok": None}
        try:
            r = get(src["url"], retries=1)
            items = parse_feed(r.content, src)
            row["entries"] = len(items)
            pubs = [i["published_at"] for i in items if i["published_at"]]
            row["no_timestamp"] = len(items) - len(pubs)
            if pubs:
                row["newest_age"] = human_age((now - max(pubs)).total_seconds())
            if not items:
                row["status"] = "EMPTY"
            else:
                try:
                    get(items[0]["url"], retries=1)
                    row["link_ok"] = True
                except FetchError as fe:
                    row["link_ok"] = False
                    row["status"] = f"LINK {fe.status or 'ERR'}"
        except Exception as ex:  # noqa: BLE001
            row["status"] = f"FAIL {getattr(ex, 'status', None) or type(ex).__name__}"
        rows.append(row)
    os.makedirs(os.environ.get("DATA_DIR", "data"), exist_ok=True)
    with open(os.path.join(os.environ.get("DATA_DIR", "data"), "audit.json"), "w") as f:
        json.dump({"at": iso(now), "rows": rows}, f, indent=1)
    kv_set(conn, "last_audit", iso(now))
    bad = [r for r in rows if r["status"] != "OK"]
    text = f"🔎 <b>Feed audit</b>: {len(rows) - len(bad)}/{len(rows)} OK\n" + \
        "\n".join(f"{'✅' if r['status'] == 'OK' else '❌'} {e(r['id'])}: {e(r['status'])}, {r['entries']} items, newest {r['newest_age'] or 'n/a'}" for r in rows)
    notifier.send_once(f"audit:{iso(now)}", "audit", text, now)
    return rows


def selftest(conn, cfg, notifier, now, get=http_get) -> dict:
    """Live acceptance exercises: link validation, duplicate suppression, Telegram test, ops alert test, briefing, sample package."""
    res = {}
    data_dir = os.environ.get("DATA_DIR", "data")
    # 1. fetch real feeds once, ingest twice: second pass must add nothing
    items = []
    for bot in ("radar", "primary"):
        for src in [s for s in cfg["sources"]["sources"] if s["bot"] == bot and s.get("enabled", True)]:
            try:
                items += parse_feed(get(src["url"], retries=1).content, src)
                mark_source(conn, src, now, True, 0)
            except Exception as ex:  # noqa: BLE001
                mark_source(conn, src, now, False, err=str(ex), http=getattr(ex, "status", None))
    first = ingest(conn, items, now, cfg)
    second = ingest(conn, items, now, cfg)
    rescore_recent(conn, now, cfg)
    res["items_fetched"] = len(items)
    res["first_pass_new"] = first["new"]
    res["second_pass_new"] = second["new"]
    dup_ok = bool(items) and second["new"] == 0 and (second["dup_url"] + second["dup_content"]) == len(items)
    # notifications duplicate suppression: same key twice -> second is 'duplicate'
    k = f"test:{iso(now)[:10]}"
    a = notifier.send_once(k, "test", "✅ <b>Telegram test</b> from THE POST-MORTEM newsroom (cloud run).", now)
    b = notifier.send_once(k, "test", "✅ duplicate - this must NOT arrive", now)
    before = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind='alert'").fetchone()["c"]
    send_alerts(conn, notifier, cfg, now, get)
    mid = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind='alert'").fetchone()["c"]
    send_alerts(conn, notifier, cfg, now, get)
    after = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind='alert'").fetchone()["c"]
    notif_dup_ok = (b == "duplicate") and (after == mid)
    res.update({"telegram_first": a, "telegram_second": b, "alerts_before": before, "alerts_after_pass1": mid, "alerts_after_pass2": after})
    kv_set(conn, "dupe_test", ("PASS " if dup_ok and notif_dup_ok else "FAIL ") + iso(now))
    # 2. validate real source links
    top = conn.execute("SELECT id FROM stories WHERE lead_only=0 ORDER BY score DESC LIMIT 4").fetchall()
    ok_links = 0
    for r in top:
        seed_claims(conn, r["id"], cfg, now)
        out = open_sources(conn, r["id"], now, get=get, max_docs=2)
        ok_links += len(out["opened"])
    res["links_opened"] = ok_links
    # 3. sample editorial package
    if top:
        build_package(conn, top[0]["id"], data_dir, now)
        res["sample_package_story"] = top[0]["id"]
    # 4. ops alert test + briefing test (separate keys so they never block real ones)
    res["ops_test"] = notifier.send_once(f"opstest:{iso(now)[:10]}", "ops", "⚠️ <b>OPS ALERT (test)</b>\nOperational alerting works.", now)
    kv_set(conn, "ops_alert_test", res["ops_test"])
    res["briefing_test"] = notifier.send_once(f"briefingtest:{iso(now)[:10]}", "briefing-test", "🧪 Briefing preview\n" + build_briefing(conn, cfg, now), now)
    kv_set(conn, "briefing_test", res["briefing_test"])
    eok, elines = editorial_selftest(now)
    kv_set(conn, "editorial_test", ("PASS " if eok else "FAIL ") + iso(now))
    res["editorial_selftest"] = elines
    record_run(conn, "selftest", now, now, "ok", detail=json.dumps({k: v for k, v in res.items() if k != "editorial_selftest"})[:480])
    return res


def editorial_selftest(now) -> tuple[bool, list[str]]:
    """Exercise the editorial controls on a THROWAWAY database (never the real one) and report each assertion."""
    import tempfile
    from .db import connect
    out, ok = [], True

    def chk(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        out.append(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as d:
        c = connect(d)
        c.execute("INSERT INTO stories(headline,headline_norm,detected_at,last_item_at,score,editorial_status) VALUES('t','t',?,?,90,'ready')", (iso(now), iso(now)))
        c.execute("INSERT INTO claims(story_id,claim_type,claim_text,status) VALUES(1,'source','s','VERIFIED')")
        c.execute("INSERT INTO claims(story_id,claim_type,claim_text,status) VALUES(1,'names','n','UNVERIFIED')")
        c.commit()
        ed.save_version(c, 1, "article", "v1 text", now)
        chk("approval refused while a claim is UNVERIFIED", not ed.approve(c, 1, now)["ok"])
        c.execute("UPDATE claims SET status='VERIFIED'")
        c.commit()
        chk("unapproved content cannot be published", not ed.mark_published(c, 1, now)["ok"])
        chk("approval recorded against version 1", ed.approve(c, 1, now)["ok"] and ed.can_publish(c, 1)[0])
        r = ed.save_version(c, 1, "article", "v1 text with a changed figure", now)
        chk("material edit voids the approval", r["invalidated"] == 1 and not ed.can_publish(c, 1)[0])
        chk("edited version cannot be published until re-approved", not ed.mark_published(c, 1, now)["ok"])
        chk("re-approval of the new version works", ed.approve(c, 1, now)["ok"] and ed.mark_published(c, 1, now)["ok"])
        chk("correction can be recorded", ed.add_correction(c, 1, "test correction", now)["ok"])
        chk("investigative lead can be recorded", ed.add_lead(c, now, "test lead")["ok"])
        chk("breaking flag auto-expires", ed.set_breaking(c, 1, now, 1)["ok"] and ed.expire_breaking(c, now + timedelta(hours=2)) == 1)
        ed.live_open(c, 1, now)
        u = ed.live_add_update(c, 1, "update", now, "https://example.org/x")["id"]
        chk("live update is not public until approved", ed.live_publishable(c, 1) == [] and ed.live_approve_update(c, u, now)["ok"] and len(ed.live_publishable(c, 1)) == 1)
        c.close()
    return ok, out


def acceptance(conn, cfg, now) -> tuple[bool, list[tuple[str, bool, str]]]:
    def q(sql, *a):
        return conn.execute(sql, a).fetchall()
    out = []
    sched = q("SELECT finished_at, run_ref FROM runs WHERE bot='radar' AND env='github-actions:schedule' AND status IN ('ok','partial') ORDER BY id")
    span = (parse_iso(sched[-1]["finished_at"]) - parse_iso(sched[0]["finished_at"])).total_seconds() / 60 if len(sched) > 1 else 0
    out.append(("Cloud scheduling active (>=3 cron-triggered radar runs over >=30 min)", len(sched) >= 3 and span >= 30, f"{len(sched)} scheduled runs, span {span:.0f} min"))
    seen = q("SELECT COALESCE(SUM(items_seen),0) n FROM runs WHERE bot IN ('radar','primary') AND status IN ('ok','partial')")[0]["n"]
    out.append(("News ingestion has executed", seen > 0, f"{seen} items seen"))
    n = q("SELECT COUNT(*) c FROM items")[0]["c"]
    out.append(("Genuine news detected and stored (>=20 items)", n >= 20, f"{n} items"))
    ok = q("SELECT COUNT(*) c FROM source_checks WHERE http_status=200")[0]["c"]
    out.append(("Source links validated (>=3 opened, HTTP 200)", ok >= 3, f"{ok} verified opens"))
    t = q("SELECT COUNT(*) c FROM notifications WHERE kind='test' AND ok=1")[0]["c"]
    out.append(("Telegram test notification accepted by Telegram (confirm it arrived on your phone)", t >= 1, f"{t} accepted"))
    out.append(("Duplicate suppression tested", (kv_get(conn, "dupe_test", "") or "").startswith("PASS"), kv_get(conn, "dupe_test", "not run")))
    pk = [r["package_path"] for r in q("SELECT package_path FROM stories WHERE package_path IS NOT NULL")]
    pk_ok = any(os.path.exists(os.path.join(os.environ.get("DATA_DIR", "data"), p, "05_part_e_report.md")) for p in pk)
    out.append(("Sample editorial package generated", pk_ok, f"{len(pk)} packages"))
    h = q("SELECT COUNT(*) c FROM runs WHERE bot='health' AND status='ok'")[0]["c"]
    out.append(("Bot health monitoring working (health runs + ops alert test)", h >= 1 and kv_get(conn, "ops_alert_test") == "sent", f"{h} health runs, ops test={kv_get(conn, 'ops_alert_test', 'not run')}"))
    wf = os.path.join(ROOT, ".github", "workflows", "briefing.yml")
    out.append(("Overnight briefing scheduled (07:00 Sydney) and preview delivered", os.path.exists(wf) and kv_get(conn, "briefing_test") == "sent", f"workflow={os.path.exists(wf)}, preview={kv_get(conn, 'briefing_test', 'not run')}"))
    off = kv_get(conn, "owner_confirmed_offline")
    out.append(("Cloud execution independently demonstrated (you sent /confirm_offline with your computer off)", bool(off) and len(sched) >= 3, f"confirmed={off or 'no'}"))
    out.append(("Editorial controls tested (version-bound approval, edit voids approval, unapproved cannot publish, corrections, leads, live approval)",
                (kv_get(conn, "editorial_test", "") or "").startswith("PASS"), kv_get(conn, "editorial_test", "not run")))
    claims = q("SELECT COUNT(*) c FROM claims")[0]["c"]
    out.append(("Claim-level fact register exists for a real detected story", claims > 0 and pk_ok, f"{claims} claims"))
    out.append(("Operating costs and limits documented", os.path.exists(os.path.join(ROOT, "docs", "COSTS.md")), "docs/COSTS.md"))
    return all(o[1] for o in out), out
