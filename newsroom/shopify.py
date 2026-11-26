"""Create DRAFT Shopify blog articles (never published, never tagged pm-approved).

Needs the Dev Dashboard app "Newsroom drafts" (scopes read_content + write_content) installed on the store, and three settings:
SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET (GitHub secrets) and SHOPIFY_STORE (a variable, e.g. skecib-y7.myshopify.com).
A short-lived access token (about 24 hours) is requested from the client credentials at run time, kept in memory only, and never printed or stored."""
from __future__ import annotations

import html
import json

import requests

API_VERSION = "2026-07"
BLOG_HANDLE = "investigations"
FORBIDDEN_TAGS = {"pm-approved"}


class ShopifyError(Exception):
    """A plain-English reason; safe to print (never contains a secret)."""


NEXT_OPTION = {
    "shop_not_permitted": "the store is not in the same Shopify organization as the app; next option: a legacy custom app made inside the store admin (Settings > Apps > Develop apps) or Shopify's authorization-code install",
    "app_not_installed": "the app is not installed on this store; next option: install it from Dev Dashboard > Apps > Newsroom drafts",
    "invalid_client": "the client ID or secret does not match the app; next option: re-copy both from the app's Settings screen into the GitHub secrets",
}


def get_token(store: str, client_id: str, client_secret: str, session=None) -> str:
    s = session or requests
    try:
        r = s.post(f"https://{store}/admin/oauth/access_token",
                   data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}, timeout=30)
    except requests.RequestException as ex:
        raise ShopifyError(f"could not reach Shopify ({type(ex).__name__}); next option: try again in a few minutes")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 200 and body.get("access_token"):
        return body["access_token"]
    code = str(body.get("error") or body.get("errors") or r.status_code)
    for k, why in NEXT_OPTION.items():
        if k in code or k in json.dumps(body):
            raise ShopifyError(f"Shopify refused the token request ({k}): {why}")
    raise ShopifyError(f"Shopify refused the token request (HTTP {r.status_code}, {code[:80]}); next option: check the app is installed and the secrets match")


class Client:
    def __init__(self, store: str, token: str, session=None):
        self.url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
        self.token, self.s = token, session or requests

    def gql(self, query: str, variables: dict | None = None) -> dict:
        r = self.s.post(self.url, json={"query": query, "variables": variables or {}}, headers={"X-Shopify-Access-Token": self.token}, timeout=40)
        if r.status_code != 200:
            raise ShopifyError(f"Shopify API answered HTTP {r.status_code}")
        d = r.json()
        if d.get("errors"):
            msg = "; ".join(str(e.get("message", e))[:120] for e in d["errors"])
            if "access denied" in msg.lower() or "scope" in msg.lower():
                raise ShopifyError("Shopify says the app lacks the content permission; next option: add read_content and write_content to the app version and approve them in the store")
            raise ShopifyError("Shopify API error: " + msg[:200])
        return d["data"]


def find_blog(c: Client) -> dict:
    blogs = c.gql("{ blogs(first: 20) { nodes { id title handle } } }")["blogs"]["nodes"]
    if not blogs:
        raise ShopifyError("the store has no blog; create one in Shopify (Content > Blog posts) first")
    return next((b for b in blogs if b["handle"] == BLOG_HANDLE), blogs[0])


def existing(c: Client) -> tuple[set[str], set[str]]:
    """Titles and YouTube ids already on the store's blogs (published or draft), so nothing is imported twice."""
    titles, vids, cursor = set(), set(), None
    while True:
        d = c.gql("query($c:String){ articles(first: 100, after:$c) { nodes { title tags } pageInfo { hasNextPage endCursor } } }", {"c": cursor})["articles"]
        for a in d["nodes"]:
            titles.add((a["title"] or "").strip().lower())
            vids.update(t[3:] for t in (a.get("tags") or []) if t.startswith("yt-"))
        if not d["pageInfo"]["hasNextPage"]:
            return titles, vids
        cursor = d["pageInfo"]["endCursor"]


def body_for(item: dict) -> str:
    return (f"<p>{html.escape(item['summary'])}</p>"
            f"<p>Video: <a href=\"{html.escape(item['youtube'])}\">{html.escape(item['youtube'])}</a></p>"
            f"<p>Originally published on YouTube on {html.escape(item['date'])}.</p>")


def run(items: list[dict], store: str, client_id: str, client_secret: str, dry_run: bool = False, session=None) -> dict:
    token = get_token(store, client_id, client_secret, session)
    c = Client(store, token, session)
    blog = find_blog(c)
    titles, vids = existing(c)
    out = {"blog": blog["title"], "created": [], "skipped_existing": [], "failed": [], "dry_run": dry_run}
    for it in items:
        vid = it["youtube"].rsplit("=", 1)[-1]
        if vid in vids or it["title"].strip().lower() in titles:
            out["skipped_existing"].append(it["title"])
            continue
        if dry_run:
            out["created"].append(it["title"])
            continue
        tags = [t.strip() for t in (it.get("tag") or "").split(",") if t.strip() and t.strip().lower() not in FORBIDDEN_TAGS] + [f"yt-{vid}", "draft-import"]
        art = {"blogId": blog["id"], "title": it["title"], "author": {"name": "The Post-Mortem"}, "body": body_for(it),
               "summary": f"<p>{html.escape(it['summary'])}</p>", "tags": tags, "isPublished": False}
        r = c.gql("mutation($a: ArticleCreateInput!){ articleCreate(article: $a){ article{ id title isPublished } userErrors{ field message } } }", {"a": art})["articleCreate"]
        if r["userErrors"] or not r["article"]:
            out["failed"].append(f"{it['title']}: {'; '.join(e['message'] for e in r['userErrors'])[:120]}")
        elif r["article"]["isPublished"]:        # defensive: must never happen
            out["failed"].append(f"{it['title']}: came back published (unexpected)")
        else:
            out["created"].append(it["title"])
    return out


def selftest(store: str, client_id: str, client_secret: str, session=None) -> dict:
    """Prove write access end to end without leaving anything behind: create ONE clearly-labelled unpublished draft, check it is a draft, delete it."""
    c = Client(store, get_token(store, client_id, client_secret, session), session)
    blog = find_blog(c)
    art = {"blogId": blog["id"], "title": "ZZ AUTOMATED TEST DRAFT (deleted immediately)", "author": {"name": "The Post-Mortem"},
           "body": "<p>Automated permission test. This draft is deleted straight away.</p>", "tags": ["automated-test"], "isPublished": False}
    r = c.gql("mutation($a: ArticleCreateInput!){ articleCreate(article: $a){ article{ id isPublished } userErrors{ message } } }", {"a": art})["articleCreate"]
    if r["userErrors"] or not r["article"]:
        raise ShopifyError("Shopify refused to create the test draft: " + "; ".join(e["message"] for e in r["userErrors"])[:150])
    aid, published = r["article"]["id"], r["article"]["isPublished"]
    d = c.gql("mutation($id: ID!){ articleDelete(id: $id){ deletedArticleId userErrors{ message } } }", {"id": aid})["articleDelete"]
    return {"blog": blog["title"], "draft_created": True, "was_published": bool(published), "deleted_again": bool(d.get("deletedArticleId")), "errors": [e["message"] for e in d["userErrors"]]}


def list_articles(store: str, client_id: str, client_secret: str, session=None) -> list[dict]:
    """READ ONLY: every blog article with its status, tags and date."""
    c = Client(store, get_token(store, client_id, client_secret, session), session)
    out, cursor = [], None
    while True:
        d = c.gql("query($c:String){ articles(first: 100, after:$c) { nodes { title handle isPublished publishedAt tags blog { handle } } pageInfo { hasNextPage endCursor } } }", {"c": cursor})["articles"]
        for a in d["nodes"]:
            out.append({"title": a["title"], "blog": (a.get("blog") or {}).get("handle"), "handle": a["handle"], "published": bool(a["isPublished"]),
                        "date": (a.get("publishedAt") or "")[:10], "pm_approved": "pm-approved" in (a.get("tags") or []), "tags": a.get("tags") or []})
        if not d["pageInfo"]["hasNextPage"]:
            return out
        cursor = d["pageInfo"]["endCursor"]


PLACEHOLDER_WORDS = ("lorem ipsum", "placeholder", "todo", "tbd", "sample text", "test article", "test draft", "xxx")
_FIELDS = "id title handle isPublished publishedAt createdAt updatedAt tags body summary blog { handle } metafields(first: 20) { nodes { namespace key value } }"


def _strip(h: str) -> str:
    import re
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def youtube_ids(text: str) -> list[str]:
    import re
    return list(dict.fromkeys(re.findall(r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})", text or "")))


def fetch_articles(c: Client) -> list[dict]:
    out, cursor = [], None
    while True:
        d = c.gql("query($c:String){ articles(first: 50, after:$c) { nodes { " + _FIELDS + " } pageInfo { hasNextPage endCursor } } }", {"c": cursor})["articles"]
        out += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return out
        cursor = d["pageInfo"]["endCursor"]


def check_article(a: dict, link_ok) -> dict:
    """Pre-publish checks: title, a date, a working YouTube link, readable body without placeholder or test text."""
    problems = []
    body = _strip(a.get("body"))
    meta = {m["key"]: m["value"] for m in (a.get("metafields") or {}).get("nodes", []) if m["namespace"] == "custom"}
    ids = youtube_ids((a.get("body") or "") + " " + " ".join(meta.values()))
    if not (a.get("title") or "").strip():
        problems.append("no title")
    if not (a.get("publishedAt") or a.get("createdAt")):
        problems.append("no date")
    if len(body) < 80:
        problems.append(f"body too short ({len(body)} characters)")
    low = (body + " " + (a.get("title") or "")).lower()
    bad = [w for w in PLACEHOLDER_WORDS if w in low]
    if bad:
        problems.append("placeholder or test text: " + ", ".join(bad))
    if not ids:
        problems.append("no YouTube link")
    elif not any(link_ok(i) for i in ids):
        problems.append("YouTube link does not open")
    return {"id": a["id"], "title": a["title"], "handle": a["handle"], "blog": (a.get("blog") or {}).get("handle"), "published": bool(a["isPublished"]),
            "publishedAt": a.get("publishedAt"), "createdAt": a.get("createdAt"), "tags": a.get("tags") or [], "body_chars": len(body), "youtube": ids[:1], "problems": problems,
            "body_says_verified": "verified" in body.lower(), "meta_sources_lines": len([x for x in (meta.get("sources") or "").splitlines() if x.strip() and "[TO ADD]" not in x]),
            "meta_keys": sorted(meta)}


def youtube_ok(vid: str) -> bool:
    try:
        r = requests.get("https://www.youtube.com/oembed", params={"format": "json", "url": f"https://www.youtube.com/watch?v={vid}"}, timeout=20)
        return r.status_code == 200
    except requests.RequestException:
        return False


def audit_articles(store: str, client_id: str, client_secret: str, session=None, link_ok=youtube_ok) -> list[dict]:
    """READ ONLY."""
    c = Client(store, get_token(store, client_id, client_secret, session), session)
    return [check_article(a, link_ok) for a in fetch_articles(c)]


def publish_articles(store: str, client_id: str, client_secret: str, session=None, link_ok=youtube_ok, only_videos: set | None = None) -> list[dict]:
    """Publish ONLY the posts that pass every check, adding the pm-approved tag and keeping all existing tags.
    Title, text, dates and links are never sent, so they are never changed."""
    c = Client(store, get_token(store, client_id, client_secret, session), session)
    res = []
    for a in fetch_articles(c):
        chk = check_article(a, link_ok)
        if only_videos is not None and not (set(chk["youtube"]) & only_videos):
            res.append(dict(chk, action="held back (not in the approved list)"))
            continue
        if chk["problems"]:
            res.append(dict(chk, action="skipped"))
            continue
        tags = list(dict.fromkeys((a.get("tags") or []) + ["pm-approved"]))
        r = c.gql("mutation($id: ID!, $a: ArticleUpdateInput!){ articleUpdate(id: $id, article: $a){ article{ id isPublished publishedAt tags } userErrors{ message } } }",
                  {"id": a["id"], "a": {"isPublished": True, "tags": tags}})["articleUpdate"]
        if r["userErrors"] or not r["article"]:
            res.append(dict(chk, action="failed", problems=[e["message"] for e in r["userErrors"]]))
        else:
            res.append(dict(chk, action="published", published=r["article"]["isPublished"], publishedAt=r["article"]["publishedAt"], tags=r["article"]["tags"]))
    return res


def unpublish_article(store: str, client_id: str, client_secret: str, handle: str, session=None) -> dict:
    """Hide one post again (the tags and text stay as they are)."""
    c = Client(store, get_token(store, client_id, client_secret, session), session)
    a = next((x for x in fetch_articles(c) if x["handle"] == handle), None)
    if not a:
        raise ShopifyError(f"no article with the address name '{handle}'")
    r = c.gql("mutation($id: ID!, $a: ArticleUpdateInput!){ articleUpdate(id: $id, article: $a){ article{ id isPublished } userErrors{ message } } }", {"id": a["id"], "a": {"isPublished": False}})["articleUpdate"]
    return {"handle": handle, "published": bool(r["article"] and r["article"]["isPublished"]), "errors": [e["message"] for e in r["userErrors"]]}
