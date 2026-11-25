"""Append-only, hash-chained change log.

Rows are written automatically by database triggers (claims, approvals, versions, corrections, status, second checks,
evidence). `seal()` then links each new row to the previous one with SHA-256, so any later edit, deletion or re-ordering
of history is detectable with `verify()`. Triggers also refuse UPDATE/DELETE of sealed rows.

Honest limit: someone with raw access to the database FILE could rebuild the whole chain. Protection against that is the
private repo's access control plus the seal hash being sent to you (see `head()`), which gives an outside witness."""
from __future__ import annotations

import hashlib


def _h(prev: str, r) -> str:
    s = "|".join(str(x) for x in (prev or "", r["id"], r["at"], r["story_id"], r["kind"], r["ref"], r["detail"]))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def seal(conn) -> int:
    """Chain every not-yet-sealed row in id order. Safe to call any time; returns how many were sealed."""
    last = conn.execute("SELECT row_hash FROM change_log WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    prev = last["row_hash"] if last else ""
    n = 0
    for r in conn.execute("SELECT * FROM change_log WHERE row_hash IS NULL ORDER BY id").fetchall():
        h = _h(prev, r)
        conn.execute("UPDATE change_log SET prev_hash=?, row_hash=? WHERE id=? AND row_hash IS NULL", (prev, h, r["id"]))
        prev, n = h, n + 1
    conn.commit()
    return n


def verify(conn) -> tuple[bool, int, int | None]:
    """(chain intact, rows checked, first bad row id)."""
    prev, n = "", 0
    for r in conn.execute("SELECT * FROM change_log WHERE row_hash IS NOT NULL ORDER BY id"):
        if r["prev_hash"] != prev or _h(prev, r) != r["row_hash"]:
            return False, n, r["id"]
        prev, n = r["row_hash"], n + 1
    return True, n, None


def head(conn) -> str:
    r = conn.execute("SELECT row_hash FROM change_log WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return r["row_hash"] if r else ""


def for_story(conn, sid: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id,at,kind,ref,detail,row_hash FROM change_log WHERE story_id=? ORDER BY id", (sid,))]
