"""HTTP + feed parsing. Retries with exponential backoff; never bypasses paywalls or access controls."""
from __future__ import annotations

import os
import random
import re
import time
from calendar import timegm
from datetime import datetime, timezone

import html as _html
import urllib.robotparser
from urllib.parse import urljoin, urlparse

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


_ROBOTS: dict = {}
_DATE_RES = [(re.compile(r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})"), "dmy"),
             (re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})"), "mdy"),
             (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "iso")]
_MONTHS = {m: i + 1 for i, m in enumerate("January February March April May June July August September October November December".split())}


def robots_allowed(url: str, get) -> bool:
    """Respect robots.txt for pages we poll (not needed for feeds meant for machines). Unreachable robots.txt = allowed (standard), a 'disallow' = never."""
    host = urlparse(url)
    root = f"{host.scheme}://{host.netloc}"
    rp = _ROBOTS.get(root)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(get(root + "/robots.txt", retries=0).text.splitlines())
        except Exception:  # noqa: BLE001
            rp.parse([])
        _ROBOTS[root] = rp
    return rp.can_fetch(user_agent(), url)


def _date_near(text: str, nearest_last: bool = False):
    """First date in the text (or, for dates that PRECEDE a headline, the one closest to the end of the text)."""
    found = []
    for rx, kind in _DATE_RES:
        for m in rx.finditer(text):
            try:
                if kind == "dmy":
                    d = datetime(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)), tzinfo=timezone.utc)
                elif kind == "mdy":
                    d = datetime(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)), tzinfo=timezone.utc)
                else:
                    d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            except ValueError:
                continue
            found.append((m.start(), d))
    if not found:
        return None
    return (max if nearest_last else min)(found, key=lambda x: x[0])[1]


def parse_html_list(content: bytes, src: dict) -> list[dict]:
    """An official press-release PAGE (no feed exists): every link whose address matches html_list.href_regex becomes an item.
    The date is read from the text right after the link when the page shows one; otherwise it stays unknown (pub_known=0)."""
    cfg = src["html_list"]
    try:
        page = content.decode("utf-8")
    except UnicodeDecodeError:
        page = content.decode("cp1252", "replace")
    rx = re.compile(cfg["href_regex"])
    out, seen = [], set()
    for m in re.finditer(r"<a[^>]*?href=[\"']?([^\"'\s>]+)[\"']?[^>]*>(.*?)</a>", page, re.S | re.I):
        href = _html.unescape(m.group(1)).split("#")[0]
        url = urljoin(src["url"], href)
        title = strip_html(m.group(2), 300)
        if title.lower() in ("view", "read more", "more", "read"):          # card layouts keep the headline in aria-label
            al = re.search(r"aria-label=[\"']([^\"']+)[\"']", m.group(0))
            title = _html.unescape(al.group(1)).strip() if al else title
        if not rx.search(url) or url in seen or len(title) < int(cfg.get("min_title", 20)) or title.lower() in ("view", "read more", "more"):
            continue
        seen.add(url)
        side = cfg.get("date_from", "after")
        if side == "url":
            um = re.search(r"(\d{4})(\d{2})(\d{2})", url)
            when = _date_near(f"{um.group(1)}-{um.group(2)}-{um.group(3)}") if um else None
        elif side == "before":
            when = _date_near(strip_html(page[max(0, m.start() - 400): m.start()], 400), nearest_last=True)
        else:
            when = _date_near(title + " " + strip_html(page[m.end(): m.end() + 700], 400))
        out.append({"title": title, "url": url, "summary": "", "published_at": when, "org": src["org"], "source_id": src["id"],
                    "bot": src["bot"], "tier": src.get("tier", "primary"), "has_image": False, "links": [],
                    "tags": _tags(src)})
        if len(out) >= int(src.get("max_items", 30)):
            break
    if not out:
        raise FetchError("no press-release links found on the page")
    return out


def _tags(src: dict) -> str:
    t = []
    if src.get("tier") == "primary" or src.get("official"):
        t.append("official-source")
    if src.get("google_news"):
        t.append("via-google-news")
    if src.get("html_list"):
        t.append("press-page")
    return ",".join(t)


def fetch_items(src: dict, get=None) -> tuple[list[dict], int]:
    """Fetch one configured source (feed, press page or Google News feed) and return (items, http status)."""
    get = get or http_get
    if src.get("html_list") and not robots_allowed(src["url"], get):
        raise FetchError("robots.txt disallows polling this page", 403)
    r = get(src["url"])
    items = parse_html_list(r.content, src) if src.get("html_list") else parse_feed(r.content, src)
    return items, r.status_code


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
                    "has_image": has_img, "links": links, "tags": _tags(src)})
    return out
