"""Approve tap -> matching Shopify article becomes Visible with pm-approved; Undo reverses it; test_only items never touch Shopify."""
from datetime import timedelta

from conftest import NOW, FakeWeb, Resp
from newsroom import editorial as ed, review, shopify
from test_review_links import PAGE, URL, WB, mk


class FakeClient:
    """Stands in for the Shopify API: one existing HIDDEN article."""
    def __init__(self):
        self.article = {"id": "gid://shopify/Article/1", "title": "T", "handle": "my-story", "isPublished": False, "publishedAt": None, "createdAt": "2026-11-23T00:00:00Z",
                        "updatedAt": "", "tags": ["Politics"], "body": "", "summary": "", "blog": {"handle": "investigations"}, "metafields": {"nodes": []}}
        self.updates = []

    def gql(self, query, variables=None):
        if "articles(" in query:
            return {"articles": {"nodes": [self.article], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        a = variables["a"]
        self.updates.append(dict(a))
        self.article["isPublished"] = a.get("isPublished", self.article["isPublished"])
        self.article["tags"] = a.get("tags", self.article["tags"])
        return {"articleUpdate": {"article": {"id": self.article["id"], "isPublished": self.article["isPublished"]}, "userErrors": []}}


def link_story(conn, sid=1, handle="my-story"):
    conn.execute("INSERT INTO story_links(story_id,kind,ref,created_at) VALUES(?,?,?,?)", (sid, "article_url", f"https://thepostmortem.org/blogs/investigations/{handle}", "2026-11-24T00:00:00Z"))
    conn.commit()


def test_real_approve_makes_article_visible_with_tag_and_undo_restores_it(conn, monkeypatch, tmp_path):
    fake = FakeClient()
    monkeypatch.setattr(shopify, "_client_from_env", lambda session=None: fake)
    mk(conn)
    link_story(conn)
    web = FakeWeb({URL: Resp(PAGE)})
    r = review.apply_action(conn, "approve", 1, NOW, str(tmp_path), get=web, wb=WB)
    assert "Visible with the pm-approved tag" in r["text"]
    assert fake.article["isPublished"] is True and fake.article["tags"] == ["Politics", "pm-approved"]
    tok = int(r["markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2])
    u = review.apply_action(conn, "undo", tok, NOW + timedelta(minutes=3))
    assert "back to how it was" in u["text"]
    assert fake.article["isPublished"] is False and fake.article["tags"] == ["Politics"]


def test_blocked_approve_never_touches_shopify(conn, monkeypatch, tmp_path):
    fake = FakeClient()
    monkeypatch.setattr(shopify, "_client_from_env", lambda session=None: fake)
    mk(conn, second=False)                                   # second check missing: Approve is refused
    link_story(conn)
    r = review.apply_action(conn, "approve", 1, NOW, str(tmp_path), get=FakeWeb({URL: Resp(PAGE)}), wb=WB)
    assert "Approved" not in r["text"] or "Cannot approve" in r["text"]
    assert fake.updates == [] and fake.article["isPublished"] is False


def test_test_only_items_can_never_get_the_tag_or_be_made_visible(conn, monkeypatch, tmp_path):
    fake = FakeClient()
    monkeypatch.setattr(shopify, "_client_from_env", lambda session=None: fake)
    tid = review.ensure_test_card(conn, NOW)
    sid = review.ensure_sample_card(conn, NOW)
    for story in (tid, sid):
        link_story(conn, story)                              # even a (wrongly) linked article must be ignored
        assert shopify.approve_sync(conn, story)["changed"] is False
        review.apply_action(conn, "approve", story, NOW, str(tmp_path))
    # a real story is forced to look like a test item after the fact: still refused
    mk(conn, sid=50, headline="Real looking story about a bank fraud case")
    link_story(conn, 50)
    conn.execute("UPDATE stories SET test_only=1 WHERE id=50")
    conn.commit()
    assert shopify.approve_sync(conn, 50) == {"changed": False, "why": "this is a test item: it never touches Shopify"}
    assert fake.updates == [] and "pm-approved" not in fake.article["tags"] and fake.article["isPublished"] is False


def test_missing_settings_or_article_changes_nothing_and_never_blocks_approval(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(shopify, "_client_from_env", lambda session=None: None)
    mk(conn)
    link_story(conn)
    r = review.apply_action(conn, "approve", 1, NOW, str(tmp_path), get=FakeWeb({URL: Resp(PAGE)}), wb=WB)
    assert "Approved" in r["text"] and "Shopify article not changed" in r["text"]
    assert ed.can_publish(conn, 1, "website")[0]


def test_set_dates_only_sends_the_publish_date(monkeypatch):
    fake = FakeClient()
    fake.article.update(isPublished=True, publishedAt="2026-11-26T13:04:20Z", body='<a href="https://www.youtube.com/watch?v=5CpvqwJFaoA">v</a>')
    monkeypatch.setattr(shopify, "get_token", lambda *a, **k: "t")
    monkeypatch.setattr(shopify, "Client", lambda store, token, session=None: fake)
    out = shopify.set_dates("s", "c", "x", {"5CpvqwJFaoA": "2026-11-23T00:10:51+00:00"})
    assert fake.updates == [{"publishDate": "2026-11-23T00:10:51+00:00"}] and out[0]["errors"] == []


def test_article_setup_creates_a_hidden_untagged_article_links_it_and_dry_runs_approve(conn, monkeypatch):
    class Fake(FakeClient):
        def __init__(self):
            super().__init__()
            self.article = None
            self.calls = []

        def gql(self, query, variables=None):
            self.calls.append(query)
            if "blogs(" in query:
                return {"blogs": {"nodes": [{"id": "gid://b/1", "title": "Investigations", "handle": "investigations"}]}}
            if "articles(" in query:
                return {"articles": {"nodes": [self.article] if self.article else [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
            a = variables["a"]
            self.article = {"id": "gid://shopify/Article/9", "title": a["title"], "handle": "company-director-sentenced", "isPublished": a["isPublished"], "publishedAt": None,
                            "createdAt": "x", "updatedAt": "", "tags": a["tags"], "body": a["body"], "summary": "", "blog": {"handle": "investigations"}, "metafields": {"nodes": []}}
            return {"articleCreate": {"article": {"id": "gid://shopify/Article/9", "handle": "company-director-sentenced", "isPublished": a["isPublished"], "tags": a["tags"]}, "userErrors": []}}
    fake = Fake()
    monkeypatch.setattr(shopify, "_client_from_env", lambda session=None: fake)
    mk(conn, sid=2302, headline="Company director sentenced after liquidators unable to verify more than 1 million")
    spec = {"story_id": 2302, "title": "Company Director Sentenced", "summary": "<p>s</p>", "body": "<p>b</p>", "tags": ["Crime and Society", "pm-approved"], "sources_lines": "A | https://x"}
    out = shopify.article_setup(conn, spec, NOW)
    assert out["status"] == "Hidden" and fake.article["isPublished"] is False and "pm-approved" not in fake.article["tags"]
    assert out["address"].endswith("/blogs/investigations/company-director-sentenced")
    assert conn.execute("SELECT ref FROM story_links WHERE story_id=2302 AND kind='article_url'").fetchone()["ref"] == out["address"]
    d = out["approve_dry_run"]
    assert d["dry_run"] and d["currently"]["visible"] is False and d["on_approve_it_would_become"]["visible"] is True and "pm-approved" in d["on_approve_it_would_become"]["tags"]
    # a dry run changes nothing
    assert fake.article["isPublished"] is False and "pm-approved" not in fake.article["tags"]
    # test_only stories are refused outright
    tid = review.ensure_test_card(conn, NOW)
    try:
        shopify.article_setup(conn, dict(spec, story_id=tid), NOW)
        raise AssertionError("should have refused")
    except shopify.ShopifyError as e:
        assert "test item" in str(e)


def test_checklist_does_not_crash_for_a_story_with_no_claims_after_a_link_run(conn, tmp_path):
    from newsroom import linkcheck as lc
    conn.execute("INSERT INTO stories(id,headline,headline_norm,source_org,detected_at,last_item_at,category,score,pub_known,evidence_ok,editorial_status) "
                 "VALUES(77,'A story with no recorded facts','a story with no recorded facts','SEC','2026-11-24T19:10:00Z','2026-11-24T19:10:00Z','Other',50,1,1,'new')")
    conn.commit()
    lc.run(conn, 77, NOW, str(tmp_path), get=FakeWeb({}), wb=lambda u: ("", "offline"))
    cl = review.checklist(conn, 77)                       # used to raise IndexError
    assert cl["flags"]["links"] is False and "nothing to check" in cl["why"]["links"]
