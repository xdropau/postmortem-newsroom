"""OPTIONAL paid LLM drafting. OFF by default: needs ENABLE_LLM_DRAFTS=1 and ANTHROPIC_API_KEY, capped per day.
Without it, 04_drafting_brief.md in each package is pasted into Claude manually (the free fallback)."""
from __future__ import annotations

import os
import re

import requests

from .db import bump_usage
from .package import CLOSING
from .util import iso


def enabled() -> bool:
    return os.environ.get("ENABLE_LLM_DRAFTS") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))


def draft(conn, cfg, story_id: int, data_dir: str, now, post=requests.post) -> dict:
    if not enabled():
        return {"status": "disabled"}
    day = iso(now)[:10]
    used = conn.execute("SELECT COALESCE(SUM(calls),0) c FROM api_usage WHERE day=? AND service='llm'", (day,)).fetchone()["c"]
    if used >= cfg["settings"]["limits"]["llm_calls_per_day"]:
        return {"status": "daily-cap-reached"}
    row = conn.execute("SELECT package_path FROM stories WHERE id=?", (story_id,)).fetchone()
    brief_path = os.path.join(data_dir, row["package_path"], "04_drafting_brief.md")
    brief = open(brief_path, encoding="utf-8").read()
    r = post("https://api.anthropic.com/v1/messages",
             headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
             json={"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 6000,
                   "messages": [{"role": "user", "content": brief}]}, timeout=120)
    bump_usage(conn, "llm", 1, day)
    if r.status_code != 200:
        return {"status": f"http-{r.status_code}"}
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    secs = dict(re.findall(r"### SECTION: (\w+)\n(.*?)(?=\n### SECTION:|\Z)", text, re.S))
    nar = secs.get("NARRATION", "").strip()
    words = len(re.findall(r"\b[\w'’-]+\b", nar))
    flags = []
    if not (170 <= words <= 190):
        flags.append(f"narration {words} words (target 170-190)")
    if CLOSING not in nar:
        flags.append("closing line missing or altered")
    out = os.path.join(data_dir, row["package_path"], "06_draft_UNREVIEWED.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("# MODEL-GENERATED DRAFT. NOT REVIEWED. Do not publish without fact-check and approval.\n"
                f"Flags: {'; '.join(flags) or 'none'}\n\n{text}\n")
    conn.execute("UPDATE stories SET script=? WHERE id=?", (nar, story_id))
    conn.commit()
    return {"status": "ok", "flags": flags}
