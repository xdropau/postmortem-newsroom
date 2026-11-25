"""Shared helpers: time, URL canonicalisation, title similarity."""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")
_TS = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime(_TS) if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, _TS).replace(tzinfo=timezone.utc)


def fmt_sydney(dt: datetime) -> str:
    return dt.astimezone(SYDNEY).strftime("%a %d %b %Y, %H:%M %Z")


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def human_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)


def extract_links(raw_html: str) -> list[str]:
    return _HREF_RE.findall(raw_html or "")


def strip_html(text: str, limit: int = 500) -> str:
    """Strip markup and truncate. External content is untrusted data and is never stored in full."""
    t = html.unescape(_TAG_RE.sub(" ", text or ""))
    t = _WS_RE.sub(" ", t).strip()
    return t[:limit]


_TRACK_EXACT = {"fbclid", "gclid", "ocid", "oc", "cmpid", "cmp", "ref", "ref_src", "smid", "taid",
                "share", "mc_cid", "mc_eid", "igshid", "sr_share", "at_medium", "at_campaign"}


def canonical_url(url: str) -> str:
    p = urlsplit((url or "").strip())
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    q = sorted((k, v) for k, v in parse_qsl(p.query)
               if not k.lower().startswith("utm_") and k.lower() not in _TRACK_EXACT)
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(q), ""))


def domain_of(url: str) -> str:
    h = urlsplit(url or "").netloc.lower()
    return h[4:] if h.startswith("www.") else h


_PUNCT = re.compile(r"[^\w\s$%.]", re.U)
STOP = set("""a an the and or of to in on for at by with from as is are was were be been it its this that these those
after over says say said new us uk au vs not no but into than their his her they he she you we our will would has have
had amid about up out more most""".split())


def norm_title(t: str) -> str:
    t = html.unescape(t or "").lower()
    t = _PUNCT.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def tokens(t: str) -> list[str]:
    return [w for w in norm_title(t).split() if w not in STOP and len(w) > 1]


def content_hash(title: str, summary: str) -> str:
    return hashlib.sha256((norm_title(title) + "|" + norm_title(summary)[:300]).encode()).hexdigest()


def similarity(a: str, b: str) -> tuple[float, int]:
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0, 0
    shared = len(ta & tb)
    jac = shared / len(ta | tb)
    if jac < 0.5:   # character similarity alone is misleading ("holds" vs "cuts"); require real word overlap first
        return jac, shared
    ratio = SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()
    return max(jac, ratio), shared


def same_story(a: str, b: str, threshold: float = 0.72) -> bool:
    s, shared = similarity(a, b)
    return s >= threshold and (shared >= 3 or s >= 0.9)
