"""HTTP + feed parsing. Retries with exponential backoff; never bypasses paywalls or access controls."""
from __future__ import annotations

import os
import random
import re
import time
from calendar import timegm
from datetime import datetime, timezone

import feedparser
import requests

from .util import extract_links, strip_html

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def user_agent() -> str:
    contact = os.environ.get("CONTACT_EMAIL", "").strip()
    base = "ThePostMortemNewsroom/1.0 (+https://thepostmortem.org"
    return base + (f"; {contact})" if contact else ")")


def http_get(url: str, *, timeout: int = 20, retries: int | None = None, backoff: float = 2.0,
             sleep=time.sleep, session=None, headers=None, max_bytes: int = 5_000_000) -> requests.Response:
    """GET with retries on network errors, 429 and 5xx. Other 4xx (403/404/410) fail immediately: we do not
    retry harder or work around access controls."""
    s = session or requests
    if retries is None:
        retries = int(os.environ.get("FETCH_RETRIES", "3"))
    h = {"User-Agent": user_agent(), "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html;q=0.8, */*;q=0.5"}
    h.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = s.get(url, headers=h, timeout=timeout)
        except requests.RequestException as e:
            last = FetchError(f"network error: {type(e).__name__}")
        else:
            if r.status_code == 200:
                if len(r.content) > max_bytes:
                    raise FetchError("response too large", 200)
                return r
            if r.status_code not in RETRY_STATUS:
                raise FetchError(f"HTTP {r.status_code}", r.status_code)
            last = FetchError(f"HTTP {r.status_code}", r.status_code)
            ra = r.headers.get("Retry-After", "")
            if ra.isdigit():
                sleep(min(int(ra), 60))
        if attempt < retries:
            sleep(backoff * (2 ** attempt) + random.uniform(0, 0.5))
    raise last or FetchError("unknown fetch failure")


def _entry_time(e):
    for k in ("published_parsed", "updated_parsed"):
        t = e.get(k)
        if t:
            return datetime.fromtimestamp(timegm(t), tz=timezone.utc)
    return None


_GN_SPLIT = re.compile(r"^(.*)\s+-\s+([^-]{2,60})$")


def parse_feed(content: bytes, src: dict) -> list[dict]:
    """Return normalised items. Summaries are stripped and truncated (we never store full articles)."""
    fp = feedparser.parse(content)
    if fp.bozo and not fp.entries:
        raise FetchError("unparseable feed")
    out = []
    require = [w.lower() for w in src.get("require_any", [])]
    for e in fp.entries[: int(src.get("max_items", 40))]:
        title = strip_html(e.get("title", "") or e.get("summary", "") or e.get("description", ""), 300)  # Bluesky items have no title
        link = (e.get("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        org = src["org"]
        if src.get("google_news"):
            m = _GN_SPLIT.match(title)
            if m:
                title = m.group(1).strip()
                org = (e.get("source", {}) or {}).get("title") or m.group(2).strip()
        raw_summary = e.get("summary", "") or e.get("description", "") or ""
        summary = strip_html(raw_summary, 500)
        if require and not any(w in (title + " " + summary).lower() for w in require):
            continue
        links = [l for l in extract_links(raw_summary)]
        has_img = bool(e.get("media_content") or e.get("media_thumbnail") or e.get("enclosures"))
        pub = _entry_time(e)
        out.append({"title": title, "url": link, "summary": summary, "published_at": pub,
                    "org": org, "source_id": src["id"], "bot": src["bot"], "tier": src.get("tier", "outlet"),
                    "has_image": has_img, "links": links})
    return out
