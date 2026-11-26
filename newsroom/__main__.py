from __future__ import annotations

import argparse
import json
import os
import sys

from . import briefing, commands, dashboard, editorial, health, tools
from .config import load_all
from .cycle import run_cycle, record_run
from .db import connect
from .notify import Notifier
from .util import utcnow


def _backup_cmds(a, data_dir) -> int:
    from . import backup
    if a.cmd == "backup":
        p = backup.make_backup(data_dir, a.out if a.out != "dashboard_out" else "backup_out", utcnow())
        print(p)
        return 0
    if a.cmd == "restore-test":
        r = backup.restore_test(a.arg)
        for c in r["checks"]:
            print(("PASS " if c["ok"] else "FAIL ") + c["check"] + (f"  [{c['detail']}]" if c["detail"] and not c["ok"] else ""))
        print("RESTORE TEST:", "PASS" if r["ok"] else "FAIL", "| backup created", r.get("created"), "| rows", r.get("counts", {}).get("stories"), "stories")
        return 0 if r["ok"] else 2
    if a.cmd == "restore":
        print(json.dumps(backup.restore(a.arg, a.out)["counts"]))
        return 0
    if a.cmd == "backup-retention":
        repo = os.environ["DATA_REPO"]
        print("deleted old weekly backups:", backup.apply_retention(repo) or "none")
        return 0
    dest = a.arg or os.path.join(os.path.expanduser("~"), "PostMortem", "backups")
    r = backup.export_latest(dest)
    for c in r["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["check"])
    print(f"Saved {r['tag']} to {r['saved_to']}  | restore test: {'PASS' if r['ok'] else 'FAIL'}")
    return 0 if r["ok"] else 2


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to cp1252
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(prog="newsroom")
    p.add_argument("cmd", choices=["cycle", "briefing", "health", "dashboard", "audit", "selftest", "acceptance", "package", "draft", "prune", "commands", "submit", "metrics", "export-evidence", "check-narration", "lead", "secondcheck", "evidence", "verify-log", "backup", "restore-test", "restore", "export-backup", "backup-retention", "site-export", "testcard", "linkwatch"])
    p.add_argument("arg", nargs="?")
    p.add_argument("--force", action="store_true")
    p.add_argument("--out", default="dashboard_out")
    a = p.parse_args(argv)
    data_dir = os.environ.get("DATA_DIR", "data")
    os.environ["DATA_DIR"] = data_dir
    if a.cmd in ("restore-test", "restore", "export-backup", "backup-retention", "backup"):
        return _backup_cmds(a, data_dir)      # these never open or create the live database
    cfg, conn, now = load_all(), connect(data_dir), utcnow()
    n = Notifier(conn)
    if not n.configured:
        print("NOTE: Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); nothing will be sent.", file=sys.stderr)
    if a.cmd == "cycle":
        out = run_cycle(conn, cfg, n, now, data_dir=data_dir)
        out["commands_handled"] = commands.poll(conn, n, now)
        print(json.dumps(out, default=str))
        return 0 if any(b["status"] != "error" for b in out["bots"]) else 1
    if a.cmd == "testcard":
        from . import review
        print(review.send_test_card(conn, n, now))
        return 0
    if a.cmd == "linkwatch":
        from . import linkcheck
        print(json.dumps(linkcheck.weekly(conn, n, now, data_dir)))
        return 0
    if a.cmd == "site-export":
        # Writes ONLY approved material to <out>/pm-newsroom.json for the website theme. It never publishes anything.
        from . import siteexport
        out = a.out if a.out != "dashboard_out" else "site_export"
        print(json.dumps(siteexport.write(conn, now, os.path.join(out, "pm-newsroom.json"))))
        return 0
    if a.cmd == "commands":
        print(commands.poll(conn, n, now))
    elif a.cmd == "briefing":
        print(briefing.maybe_send(conn, cfg, n, now, force=a.force))
    elif a.cmd == "health":
        print(json.dumps(health.run_health(conn, cfg, n, now), default=str))
    elif a.cmd == "dashboard":
        print(dashboard.write_dashboard(conn, cfg, now, a.out))
    elif a.cmd == "audit":
        rows = tools.audit(conn, cfg, n, now)
        for r in rows:
            print(f"{r['status']:10} {r['id']:22} entries={r['entries']:3} newest={r['newest_age']}")
    elif a.cmd == "selftest":
        print(json.dumps(tools.selftest(conn, cfg, n, now), default=str, indent=1))
    elif a.cmd == "acceptance":
        ok, rows = tools.acceptance(conn, cfg, now)
        for name, passed, detail in rows:
            print(f"{'PASS' if passed else 'FAIL'}  {name}  [{detail}]")
        print("\nSYSTEM OPERATIONAL: " + ("YES" if ok else "NO - do not claim operational until every line passes"))
        return 0 if ok else 2
    elif a.cmd == "metrics":
        print(json.dumps(editorial.latency_metrics(conn), indent=1))
    elif a.cmd == "export-evidence":
        print(editorial.export_evidence(conn, a.out if a.out != "dashboard_out" else os.path.join(data_dir, "exports"), public_only=False))
    elif a.cmd == "check-narration":
        print(json.dumps(editorial.check_narration(open(a.arg, encoding="utf-8").read()), indent=1))
    elif a.cmd == "lead":
        print(editorial.add_lead(conn, now, **json.loads(a.arg)))
    elif a.cmd == "submit":
        # submit '<json file>': pushes drafts to a private-data-repo branch inbox-<ts>; the cloud cycle applies them.
        from .submit import submit_ops
        print(submit_ops(a.arg))
    elif a.cmd == "secondcheck":
        from . import secondcheck
        secondcheck.run(conn, int(a.arg), now)
        secondcheck.write_part_e(conn, int(a.arg), data_dir)
        print(secondcheck.telegram_text(conn, int(a.arg)))
    elif a.cmd == "evidence":
        from . import evidence
        rows = evidence.archive_story(conn, int(a.arg), now, data_dir, cfg)
        print(evidence.bundle(conn, int(a.arg), now, data_dir), f"({len(rows)} sources)")
    elif a.cmd == "verify-log":
        from . import changelog
        changelog.seal(conn)
        print(changelog.verify(conn), "head", changelog.head(conn)[:16])
    elif a.cmd == "package":
        from .package import build_package
        print(build_package(conn, int(a.arg), data_dir, now))
    elif a.cmd == "draft":
        from . import draft
        print(draft.draft(conn, cfg, int(a.arg), data_dir, now))
    elif a.cmd == "prune":
        from .util import iso
        from datetime import timedelta
        cut = iso(now - timedelta(days=365))   # long retention: only old, low-score, never-reviewed items
        ids = [r["id"] for r in conn.execute("SELECT id FROM stories WHERE last_item_at<? AND editorial_status='new' AND (score<50 OR lead_only=1) AND id NOT IN (SELECT story_id FROM approvals) "
            "AND id NOT IN (SELECT story_id FROM evidence_archive) AND id NOT IN (SELECT story_id FROM content_versions)", (cut,))]
        for sid in ids:
            conn.execute("DELETE FROM items WHERE story_id=?", (sid,))
            conn.execute("DELETE FROM claims WHERE story_id=?", (sid,))
            conn.execute("DELETE FROM stories WHERE id=?", (sid,))
        conn.commit()
        print(f"pruned {len(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
