import json

import pytest

from newsroom import shopify

ITEMS = [{"status": "DRAFT - not published", "title": "Sample investigation A", "date": "2026-11-20", "youtube": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
          "tag": "Cybersecurity, pm-approved", "summary": "One line."},
         {"status": "DRAFT - not published", "title": "Sample investigation B", "date": "2026-11-21", "youtube": "https://www.youtube.com/watch?v=BBBBBBBBBBB",
          "tag": "Politics", "summary": "Another line."}]


class R:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeShopify:
    def __init__(self, token_reply=None, have=()):
        self.token_reply = token_reply or R({"access_token": "SECRET-TOKEN", "expires_in": 86399})
        self.have, self.created, self.calls = have, [], []

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.calls.append((url, data, json, headers))
        if url.endswith("/oauth/access_token"):
            return self.token_reply
        q = json["query"]
        if "blogs(" in q:
            return R({"data": {"blogs": {"nodes": [{"id": "gid://b/1", "title": "Investigations", "handle": "investigations"}]}}})
        if "articles(" in q:
            return R({"data": {"articles": {"nodes": [{"title": t, "tags": []} for t in self.have], "pageInfo": {"hasNextPage": False, "endCursor": None}}}})
        art = json["variables"]["a"]
        self.created.append(art)
        return R({"data": {"articleCreate": {"article": {"id": "gid://a/1", "title": art["title"], "isPublished": art["isPublished"]}, "userErrors": []}}})


def test_drafts_are_never_published_never_tagged_approved_and_token_stays_out_of_output():
    s = FakeShopify()
    out = shopify.run(ITEMS, "x.myshopify.com", "cid", "csecret", session=s)
    assert len(out["created"]) == 2 and not out["failed"]
    assert all(a["isPublished"] is False for a in s.created)
    assert all("pm-approved" not in a["tags"] for a in s.created) and "yt-AAAAAAAAAAA" in s.created[0]["tags"]
    assert "SECRET-TOKEN" not in json.dumps(out) and "csecret" not in json.dumps(out)
    assert s.calls[0][1]["grant_type"] == "client_credentials"
    assert "youtube.com/watch?v=AAAAAAAAAAA" in s.created[0]["body"]


def test_existing_items_are_skipped_and_dry_run_creates_nothing():
    s = FakeShopify(have=["sample investigation a"])
    out = shopify.run(ITEMS, "x.myshopify.com", "cid", "csecret", dry_run=True, session=s)
    assert out["skipped_existing"] == ["Sample investigation A"] and out["created"] == ["Sample investigation B"] and s.created == []


@pytest.mark.parametrize("err", ["shop_not_permitted", "app_not_installed", "invalid_client"])
def test_token_failure_stops_with_one_plain_line_and_a_next_option(err):
    s = FakeShopify(token_reply=R({"error": err}, 400))
    with pytest.raises(shopify.ShopifyError) as e:
        shopify.run(ITEMS, "x.myshopify.com", "cid", "csecret", session=s)
    assert "next option" in str(e.value) and "csecret" not in str(e.value) and "\n" not in str(e.value)
    assert s.created == []
