"""Owner-side: send drafts, leads, corrections and links to the cloud system WITHOUT touching its database.
Writes inbox.json to a fresh branch `inbox-<timestamp>` of the PRIVATE data repo. The next 15-minute cloud cycle applies it
(newsroom.editorial.process_inbox) and deletes the branch. Needs `gh auth login`; uses your own GitHub login, no extra secrets."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone


def _run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"command failed: {' '.join(cmd[:3])}...\n{r.stderr.strip()[:300]}")
    return r.stdout.strip()


def submit_ops(path: str) -> str:
    ops = json.load(open(path, encoding="utf-8"))
    ops = ops["ops"] if isinstance(ops, dict) else ops
    owner = _run(["gh", "api", "user", "-q", ".login"])
    repo = os.environ.get("DATA_REPO") or f"{owner}/postmortem-newsroom-data"
    branch = "inbox-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    with tempfile.TemporaryDirectory() as d:
        _run(["gh", "repo", "clone", repo, d, "--", "--depth", "1", "-q"])
        _run(["git", "checkout", "-q", "--orphan", branch], d)
        for name in os.listdir(d):          # orphan branch holds ONLY inbox.json
            if name != ".git":
                p = os.path.join(d, name)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
        with open(os.path.join(d, "inbox.json"), "w", encoding="utf-8") as f:
            json.dump({"ops": ops}, f, indent=1)
        _run(["git", "add", "-A"], d)
        _run(["git", "-c", "user.name=owner", "-c", "user.email=owner@users.noreply.github.com", "commit", "-q", "-m", f"inbox {branch}"], d)
        _run(["git", "push", "-q", "origin", f"{branch}:{branch}"], d)
    return f"submitted {len(ops)} op(s) as {repo}@{branch}; applied by the next cloud cycle (within ~15 min)"
