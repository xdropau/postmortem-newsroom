"""Seconds since the newest cycle run was created / finished. Reads `gh run list --json createdAt,updatedAt,status` JSON on stdin.
Prints 9999999 when there is none. Prints no story text (there is none in run metadata)."""
import datetime as D
import json
import sys

kind = sys.argv[1] if len(sys.argv) > 1 else "created"
try:
    runs = json.load(sys.stdin)
    stamps = [x["createdAt"] if kind == "created" else x["updatedAt"] for x in runs if kind == "created" or x["status"] == "completed"]
    now = D.datetime.now(D.timezone.utc)
    print(int((now - max(D.datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)).total_seconds()) if stamps else 9999999)
except Exception:  # noqa: BLE001 - a failed lookup must never stop the clock
    print(9999999)
