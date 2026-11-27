"""Weekly backups, restore test, retention and offline export. PRIVATE data only; contains no secrets (tokens live in Actions
secrets, never in the database or evidence folders)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import date, datetime

from . import changelog

KEEP_WEEKLY = 12
TABLES = ["stories", "items", "claims", "approvals", "content_versions", "corrections", "evidence_archive", "second_checks",
          "second_check_items", "change_log", "leads", "live_coverage", "live_updates", "story_links", "story_notes", "notifications"]
INCLUDE_DIRS = ["evidence", "packages", "exports"]


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(db_path: str) -> dict:
    c = sqlite3.connect(db_path)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()}
    finally:
        c.close()


def make_backup(data_dir: str, out_dir: str, now: datetime) -> str:
    """Consistent copy of the database (SQLite online-backup API) + evidence files, gzip-compressed, with a manifest."""
    os.makedirs(out_dir, exist_ok=True)
    name = f"postmortem-backup-{now.strftime('%Y-%m-%d')}.tar.gz"
    out = os.path.join(out_dir, name)
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, "newsroom.db")
        src = sqlite3.connect(os.path.join(data_dir, "newsroom.db"))
        dst = sqlite3.connect(snap)
        src.backup(dst)
        dst.close()
        src.close()
        conn = sqlite3.connect(snap)
        conn.row_factory = sqlite3.Row
        changelog.seal(conn)
        ok, n, bad = changelog.verify(conn)
        head = changelog.head(conn)
        conn.close()
        files = {"newsroom.db": snap}
        for d in INCLUDE_DIRS:
            base = os.path.join(data_dir, d)
            for root, _, fs in os.walk(base):
                for f in fs:
                    full = os.path.join(root, f)
                    files[os.path.relpath(full, data_dir).replace(os.sep, "/")] = full
        manifest = {"created": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "format": 1, "counts": _counts(snap),
                    "changelog": {"intact": ok, "rows": n, "bad_row": bad, "head": head},
                    "files": {rel: {"sha256": _sha(p), "bytes": os.path.getsize(p)} for rel, p in sorted(files.items())}}
        mpath = os.path.join(tmp, "MANIFEST.json")
        json.dump(manifest, open(mpath, "w"), indent=1)
        with tarfile.open(out, "w:gz", compresslevel=9) as tar:
            tar.add(mpath, arcname="MANIFEST.json")
            for rel, p in sorted(files.items()):
                tar.add(p, arcname=rel)
    with open(out + ".sha256", "w") as f:
        f.write(f"{_sha(out)}  {name}\n")
    return out


def _safe_extract(tar: tarfile.TarFile, dest: str):
    for m in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, m.name))
        if not target.startswith(os.path.realpath(dest) + os.sep) or m.issym() or m.islnk():
            raise ValueError(f"unsafe path in archive: {m.name}")
    tar.extractall(dest, filter="data")


def restore(archive: str, dest: str) -> dict:
    """Extract an archive to `dest` (refuses a non-empty destination so a live database is never overwritten)."""
    if os.path.exists(dest) and os.listdir(dest):
        raise SystemExit(f"{dest} is not empty; restore into a new folder")
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        _safe_extract(tar, dest)
    return json.load(open(os.path.join(dest, "MANIFEST.json")))


def restore_test(archive: str) -> dict:
    """Restore into a temp folder and PROVE it: file hashes, SQLite integrity, row counts, change-log chain, sample query."""
    res = {"archive": os.path.basename(archive), "checks": [], "ok": False}

    def chk(name, cond, detail=""):
        res["checks"].append({"check": name, "ok": bool(cond), "detail": str(detail)})
        return cond

    sidecar = archive + ".sha256"
    if os.path.exists(sidecar):
        want = open(sidecar).read().split()[0]
        chk("archive sha256 matches its .sha256 file", _sha(archive) == want)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            m = restore(archive, os.path.join(tmp, "r"))
        except Exception as ex:  # noqa: BLE001
            chk("archive extracts", False, ex)
            return res
        root = os.path.join(tmp, "r")
        bad = [rel for rel, meta in m["files"].items() if not os.path.exists(os.path.join(root, rel)) or _sha(os.path.join(root, rel)) != meta["sha256"]]
        chk(f"all {len(m['files'])} files present with matching SHA-256", not bad, bad[:3])
        db = os.path.join(root, "newsroom.db")
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        chk("SQLite integrity_check", c.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        got = _counts(db)
        chk("row counts equal the manifest", got == m["counts"], {k: (got.get(k), v) for k, v in m["counts"].items() if got.get(k) != v})
        ok, n, badrow = changelog.verify(c)
        chk(f"change-log hash chain intact ({n} rows)", ok and n == m["changelog"]["rows"], badrow)
        chk("change-log head equals manifest", changelog.head(c) == m["changelog"]["head"])
        s = c.execute("SELECT COUNT(*) n, MAX(id) mx FROM stories").fetchone()
        chk("restored database is queryable", s["n"] == m["counts"]["stories"], f"{s['n']} stories")
        c.close()
        res["counts"] = m["counts"]
        res["created"] = m["created"]
    res["ok"] = all(x["ok"] for x in res["checks"])
    return res


def to_delete(tag_dates: list[date], keep_weekly: int = KEEP_WEEKLY) -> list[date]:
    """Retention: keep the newest `keep_weekly` backups AND the first backup of every calendar month, forever."""
    ds = sorted(set(tag_dates), reverse=True)
    keep = set(ds[:keep_weekly])
    first_of_month: dict[tuple, date] = {}
    for d in sorted(ds):
        first_of_month.setdefault((d.year, d.month), d)
    keep |= set(first_of_month.values())
    return [d for d in ds if d not in keep]


def _gh(*args, check=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def apply_retention(repo: str, dry: bool = False) -> list[str]:
    tags = json.loads(_gh("release", "list", "--repo", repo, "--limit", "300", "--json", "tagName") or "[]")
    dates = {}
    for t in tags:
        if t["tagName"].startswith("backup-"):
            try:
                dates[date.fromisoformat(t["tagName"][7:])] = t["tagName"]
            except ValueError:
                pass
    dead = [dates[d] for d in to_delete(list(dates))]
    for tag in dead:
        if not dry:
            _gh("release", "delete", tag, "--repo", repo, "--cleanup-tag", "--yes")
    return dead


def export_latest(dest: str, repo: str | None = None) -> dict:
    """One command for the laptop: download the newest backup release asset, verify it, and prove it restores."""
    repo = repo or os.environ.get("DATA_REPO") or f"{_gh('api', 'user', '-q', '.login')}/postmortem-newsroom-data"
    rel = json.loads(_gh("release", "list", "--repo", repo, "--limit", "30", "--json", "tagName,publishedAt") or "[]")
    rel = sorted([r for r in rel if r["tagName"].startswith("backup-")], key=lambda r: r["tagName"], reverse=True)
    if not rel:
        raise SystemExit(f"No backup release exists yet in {repo}. Create one now with:  gh workflow run backup.yml -R {repo}")
    tag = rel[0]["tagName"]
    os.makedirs(dest, exist_ok=True)
    _gh("release", "download", tag, "--repo", repo, "--dir", dest, "--clobber")
    arc = os.path.join(dest, f"postmortem-{tag}.tar.gz")
    res = restore_test(arc)
    res["repo"], res["tag"], res["saved_to"] = repo, tag, arc
    return res
