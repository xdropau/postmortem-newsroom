"""Approve as ONE end-to-end action: check -> publish -> verify -> one plain message.

  1. CHECK    sources, facts, dates, links, second check all pass (the link check is re-run first, so an old one never blocks).
  2. PUBLISH  approve in the newsroom, make the Shopify article Visible with pm-approved, publish the public feed right now.
  3. VERIFY   open the live article address; every section heading and every source link must be on the page.
  4. REPLY    exactly one message:  LIVE: <address>   or   FAILED at step <n>: <reason>. Send /golive <id> to retry.

Safe to repeat: a retry never approves twice and never touches another story. Test items are refused."""
from __future__ import annotations

import html
import os
import re
import subprocess
import time

from . import editorial as ed, linkcheck, review, shopify
from .fetch import http_get
from .util import strip_html


def enabled() -> bool:
    """The full chain needs the cloud settings (Shopify + the GitHub token that publishes the feed). Without them the old Approve path is used."""
    return bool(os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY") and os.environ.get("SHOPIFY_CLIENT_ID") and os.environ.get("SHOPIFY_STORE"))


def applies(conn, sid: int) -> bool:
    """Use the one-action chain only where there is a website article to publish and check (otherwise the plain feed-only Approve is used)."""
    return enabled() and bool(shopify._article_handle(conn, sid))


def _fail(sid, step, why):
    why = re.sub(r"\s+", " ", str(why)).strip().rstrip(".")
    return {"ok": False, "step": step, "message": f"FAILED at step {step}: {why}. Send /golive {sid} to retry."}


def publish_feed() -> tuple[bool, str]:
    try:
        r = subprocess.run(["bash", "scripts/publish_site.sh"], capture_output=True, text=True, timeout=240)
    except Exception as ex:  # noqa: BLE001
        return False, f"{type(ex).__name__}"
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-200:]


def _expected(body: str) -> tuple[list[str], list[str]]:
    heads = [strip_html(h, limit=200) for h in re.findall(r"<h2[^>]*>(.*?)</h2>", body or "", flags=re.S)]
    links = [html.unescape(u) for u in re.findall(r'<a [^>]*href="(https?://[^"]+)"', body or "")]
    return heads, links


def verify_live(address: str, body: str, get=http_get, wait=time.sleep, tries: int = 4) -> tuple[bool, str]:
    heads, links = _expected(body)
    status, page = None, ""
    for i in range(tries):
        try:
            r = get(address + ("&" if "?" in address else "?") + f"gl={int(time.time())}", timeout=30, retries=1)
            status, page = r.status_code, html.unescape(r.text or "")
            if status == 200 and heads and all(h.lower() in page.lower() for h in heads):
                break
        except Exception as ex:  # noqa: BLE001
            status = getattr(ex, "status", None) or type(ex).__name__
        if i < tries - 1:
            wait(10)
    if status != 200:
        return False, f"the live page did not open (HTTP {status})"
    miss_h = [h for h in heads if h.lower() not in page.lower()]
    miss_l = [u for u in links if u not in page]
    if not heads:
        return False, "the article has no sections to check"
    if miss_h or miss_l:
        return False, f"the live page is missing {len(miss_h)} section(s) and {len(miss_l)} source link(s)"
    return True, f"{len(heads)} sections and {len(links)} source links are on the page"


def run(conn, sid: int, now, data_dir: str = "data", cfg=None, get=http_get, publish=publish_feed, ensure=None, wait=time.sleep, wb=None) -> dict:
    """Never raises; always returns {ok, step, message}."""
    ensure = ensure or shopify.ensure_visible
    step = 1
    try:
        if ed.is_test_only(conn, sid):
            return _fail(sid, 1, "this is a test item and can never go live")
        if not conn.execute("SELECT 1 FROM stories WHERE id=?", (sid,)).fetchone():
            return _fail(sid, 1, f"story {sid} does not exist")
        # 1. CHECK
        if cfg is None:
            from .config import load_all
            cfg = load_all()
        linkcheck.run(conn, sid, now, data_dir, cfg, get=get, wb=wb)
        cl = review.checklist(conn, sid)
        if not cl["all_ok"]:
            k = next(k for k, _ in review.CHECKS if not cl["flags"][k])
            label = dict(review.CHECKS)[k]
            return _fail(sid, 1, f"{label} is not ready ({cl['why'].get(k, 'not yet')})")
        # 2. PUBLISH
        step = 2
        st = conn.execute("SELECT editorial_status FROM stories WHERE id=?", (sid,)).fetchone()["editorial_status"]
        if st not in ("approved", "published"):
            r = review.apply_action(conn, "approve", sid, now, data_dir, cfg, get=get, wb=wb)
            if "Approved" not in (r.get("text") or ""):
                why = (review.checklist(conn, sid)["missing"] or ["the approval was refused"])[0]
                return _fail(sid, 2, "the approval was refused: " + why)
        sh = ensure(conn, sid)
        if not sh.get("ok"):
            return _fail(sid, 2, "the Shopify article could not be made Visible: " + str(sh.get("why", "unknown")))
        ok, out = publish()
        if not ok:
            return _fail(sid, 2, "the public feed could not be published")
        # 3. VERIFY
        step = 3
        address = f"https://thepostmortem.org/blogs/investigations/{shopify._article_handle(conn, sid)}"
        ok, why = verify_live(address, sh.get("body", ""), get=get, wait=wait)
        if not ok:
            return _fail(sid, 3, why)
        conn.execute("INSERT INTO runs(bot,started_at,finished_at,status,detail) VALUES('golive',?,?,'ok',?)", (now.isoformat(), now.isoformat(), f"{ed.uid(sid)} live: {why}"[:300]))
        conn.commit()
        return {"ok": True, "step": 4, "message": f"LIVE: {address}"}
    except Exception as ex:  # noqa: BLE001
        return _fail(sid, step, f"unexpected error ({type(ex).__name__})")
