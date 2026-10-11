"""Offline end-to-end tests for the merchandising flow (mock Arcade + mock IG)."""
from __future__ import annotations

import os

os.environ.setdefault("MOCK_MODE", "1")
os.environ.pop("ARCADE_API_KEY", None)

from app.bot import MerchBot, format_caption  # noqa: E402
from app.intent import parse_intent, pick_category  # noqa: E402
from app.mock_arcade import MockArcadeClient  # noqa: E402
from app.instagram import MockInstagramClient, extract_messages, verify_signature  # noqa: E402


def test_intent_parses_prompt_and_hint():
    intent = parse_intent("Make me a canvas tote with desert cacti, x2 please")
    assert intent.category_hint == "tote"
    assert "cacti" in intent.prompt
    assert "x2" not in intent.prompt  # quantity markers must not leak into the brief
    assert intent.quantity == 2


def test_poster_hint_beats_canvas_tote_name():
    cats = MockArcadeClient().list_categories()
    assert pick_category(cats, "poster")["name"] == "Art Poster"


def test_pick_category_matches_hint():
    cats = MockArcadeClient().list_categories()
    assert pick_category(cats, "mug")["name"] == "Ceramic Mug"
    # unknown hint falls back to a print category
    assert pick_category(cats, None)["world"] == "print"


def test_full_flow_returns_image_and_checkout():
    arcade = MockArcadeClient()
    ig = MockInstagramClient()
    bot = MerchBot(arcade=arcade, instagram=ig)

    result = bot.handle_message("user-1", "a poster with neon mountains")

    assert result["ok"] is True
    assert result["product"]["imageUrl"].startswith("http")
    assert result["checkout"]["checkoutUrl"].startswith("https://checkout.arcade.ai/")
    # the bot should have sent an ack, an image and the caption
    kinds = [m["type"] for m in ig.sent]
    assert kinds == ["text", "image", "text"]
    assert "checkout.arcade.ai" in ig.sent[-1]["text"]


def test_caption_includes_price_and_link():
    product = {
        "title": "Test Mug",
        "selectionLabel": "11oz / white",
        "unitPrice": {"amount": "22.00", "currency": "USD"},
        "description": "A lovely mug.",
    }
    caption = format_caption(product, 1, {"checkoutUrl": "https://checkout.arcade.ai/x"})
    assert "22.00" in caption
    assert "checkout.arcade.ai" in caption


def test_empty_message_is_handled():
    bot = MerchBot(arcade=MockArcadeClient(), instagram=MockInstagramClient())
    assert bot.handle_message("user-2", "   ")["ok"] is False


def test_webhook_extraction_ignores_echo():
    payload = {
        "entry": [
            {"messaging": [
                {"sender": {"id": "1"}, "message": {"text": "hi"}},
                {"sender": {"id": "bot"}, "message": {"text": "self", "is_echo": True}},
            ]}
        ]
    }
    msgs = extract_messages(payload)
    assert msgs == [{"sender_id": "1", "text": "hi"}]


def test_signature_skipped_without_secret():
    assert verify_signature(b"{}", None, "") is True


# -- comment-to-DM ---------------------------------------------------------

from app.instagram import extract_comments  # noqa: E402


def test_comment_triggers_private_reply():
    ig = MockInstagramClient()
    bot = MerchBot(arcade=MockArcadeClient(), instagram=ig)

    result = bot.handle_comment("cmt-1", "link please — a poster with neon mountains", commenter_id="u9", username="fan")

    assert result["ok"] is True
    assert result["via"] == "comment"
    vias = [m["via"] for m in ig.sent]
    assert vias == ["private_reply", "private_reply"]  # image + caption
    assert "checkout.arcade.ai" in ig.sent[-1]["text"]


def test_keyword_only_comment_asks_for_brief_not_invents_product():
    ig = MockInstagramClient()
    bot = MerchBot(arcade=MockArcadeClient(), instagram=ig)
    result = bot.handle_comment("cmt-k", "please send me the link")
    assert result["ok"] is True
    assert result.get("asked_for_brief") is True
    assert len(ig.sent) == 1 and ig.sent[0]["via"] == "private_reply"
    assert "brief" in ig.sent[0]["text"].lower()


def test_comment_design_brief_triggers_even_without_keyword():
    ig = MockInstagramClient()
    bot = MerchBot(arcade=MockArcadeClient(), instagram=ig)
    # no trigger keyword, but it names a product category
    result = bot.handle_comment("cmt-2", "a tote with cacti")
    assert result["ok"] is True
    assert [m["via"] for m in ig.sent] == ["private_reply", "private_reply"]


def test_comment_without_trigger_is_ignored():
    ig = MockInstagramClient()
    bot = MerchBot(arcade=MockArcadeClient(), instagram=ig)
    result = bot.handle_comment("cmt-3", "love this post 😍")
    assert result["ok"] is False
    assert result["skipped"] == "no trigger keyword"
    assert ig.sent == []


def test_same_comment_not_handled_twice():
    ig = MockInstagramClient()
    bot = MerchBot(arcade=MockArcadeClient(), instagram=ig)
    bot.handle_comment("cmt-4", "link please")
    again = bot.handle_comment("cmt-4", "link please")
    assert again["ok"] is False
    assert again["skipped"] == "already handled"


def test_extract_comments_parses_webhook():
    payload = {
        "object": "instagram",
        "entry": [{"id": "IGID", "changes": [{"field": "comments", "value": {
            "from": {"id": "fan-1", "username": "fan"},
            "media": {"id": "media-1"},
            "id": "cmt-9",
            "text": "LINK please",
        }}]}],
    }
    comments = extract_comments(payload)
    assert comments == [{
        "comment_id": "cmt-9", "commenter_id": "fan-1",
        "username": "fan", "text": "LINK please", "media_id": "media-1",
    }]


def test_extract_comments_ignores_own_replies():
    payload = {"entry": [{"id": "IGID", "changes": [{"field": "comments", "value": {
        "from": {"id": "IGID", "username": "me"}, "id": "c", "text": "hi"}}]}]}
    assert extract_comments(payload) == []


# -- product builder -------------------------------------------------------

from app.builder import BuildError, generate_product, get_options, create_checkout  # noqa: E402


def test_options_expose_values_and_resolved_selection():
    opts = get_options(MockArcadeClient(), "mock-cat-tee")
    assert opts["world"] == "print"
    keys = [o["key"] for o in opts["options"]]
    assert "size" in keys and "color" in keys
    assert set(opts["selections"]) == set(keys)  # a complete selection


def test_generate_print_product_uses_pattern_then_product():
    arcade = MockArcadeClient()
    out = generate_product(arcade, "mock-cat-tee", "retro mountain sunrise", selections={"color": "black"})
    assert out["product"]["categoryId"] == "mock-cat-tee"
    assert out["product"]["selections"]["color"] == "black"
    assert out["product"]["imageUrl"].startswith("http")
    assert out["patternId"]


def test_generate_jewelry_uses_single_pattern_call():
    arcade = MockArcadeClient()
    out = generate_product(arcade, "mock-cat-ring", "a gold ring with a tiny star")
    assert out["category"]["world"] == "jewelry"
    assert out["product"]["categoryId"] == "mock-cat-ring"


def test_generate_requires_prompt():
    try:
        generate_product(MockArcadeClient(), "mock-cat-tee", "   ")
    except BuildError as e:
        assert "prompt" in str(e).lower()
    else:
        raise AssertionError("expected BuildError")


def test_builder_checkout_returns_url():
    arcade = MockArcadeClient()
    out = generate_product(arcade, "mock-cat-mug", "mushrooms")
    co = create_checkout(arcade, out["product"]["productId"], 1)
    assert co["checkoutUrl"].startswith("https://checkout.arcade.ai/")


# -- Arcade client contract ------------------------------------------------

import httpx  # noqa: E402
from app.arcade import ArcadeClient, ArcadeError, dropship_for  # noqa: E402


def test_dropship_only_true_for_quantity_one():
    assert dropship_for([{"productId": "p", "quantity": 1}]) is True
    assert dropship_for([{"productId": "p", "quantity": 3}]) is False
    assert dropship_for([{"productId": "p", "quantity": 3}, {"productId": "q", "quantity": 1}]) is True


def test_structured_error_is_parsed():
    c = ArcadeClient(api_key="x")
    r = httpx.Response(401, json={"code": "UNAUTHORIZED", "status": 401, "message": "Order not found."})
    e = c._parse_error(r)
    assert e.status == 401 and e.code == "UNAUTHORIZED"
    assert "Order not found." in str(e)


def test_edge_403_hints_user_agent():
    c = ArcadeClient(api_key="x")
    r = httpx.Response(403, text="<html>forbidden</html>")
    e = c._parse_error(r)
    assert e.status == 403 and "User-Agent" in str(e)


def test_missing_key_fails_fast(monkeypatch):
    # don't depend on whatever key happens to be in .env
    monkeypatch.setattr("app.arcade.Settings.ARCADE_API_KEY", "")
    c = ArcadeClient(api_key="")
    try:
        c.list_categories()
    except ArcadeError as e:
        assert "ARCADE_API_KEY" in str(e)
    else:
        raise AssertionError("expected ArcadeError")


def test_retries_on_503_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"code": "SERVICE_UNAVAILABLE", "status": 503, "message": "down"})
        return httpx.Response(200, json={"items": [{"categoryId": "c", "name": "n", "world": "print"}]})

    monkeypatch.setattr("app.arcade.httpx.post", fake_post)
    slept = []
    c = ArcadeClient(api_key="k", sleep=lambda s: slept.append(s))
    items = c.list_categories()
    assert len(items) == 1 and calls["n"] == 2 and slept == [1]


# -- caching ---------------------------------------------------------------

from app.cache import CachedArcade, TTLCache, stable_key, warm  # noqa: E402


class _CountingArcade:
    """Stand-in that counts calls so we can prove what is/ isn't cached."""
    def __init__(self):
        self.calls = {"categories": 0, "models": 0, "options": 0, "pattern": 0}

    def list_categories(self):
        self.calls["categories"] += 1
        return [{"categoryId": "c1", "name": "Napkins", "world": "print"},
                {"categoryId": "c2", "name": "Rings", "world": "jewelry"}]

    def list_models(self):
        self.calls["models"] += 1
        return [{"modelId": "m1", "name": "Toile"}]

    def get_category(self, category_id, selections=None):
        self.calls["options"] += 1
        return {"categoryId": category_id, "selections": selections or {}}

    def create_pattern(self, *a, **k):
        self.calls["pattern"] += 1
        return {"jobId": "j"}


def test_ttl_cache_expires():
    now = [1000.0]
    c = TTLCache(ttl=10, clock=lambda: now[0])
    c.set("k", "v")
    assert c.get("k") == "v"
    now[0] += 11
    assert c.get("k") is None


def test_stable_key_is_order_independent():
    assert stable_key("o", "c1", {"a": 1, "b": 2}) == stable_key("o", "c1", {"b": 2, "a": 1})


def test_cached_arcade_memoises_reads_but_not_writes():
    fake = _CountingArcade()
    c = CachedArcade(fake, TTLCache(ttl=999))

    c.list_categories(); c.list_categories()
    c.list_models(); c.list_models()
    c.get_category("c1", {}); c.get_category("c1", {})     # same key -> 1 call
    c.get_category("c1", {"size": "m"})                    # different key -> 2nd call
    c.create_pattern("p"); c.create_pattern("p")           # never cached

    assert fake.calls == {"categories": 1, "models": 1, "options": 2, "pattern": 2}


def test_warm_populates_and_is_idempotent():
    fake = _CountingArcade()
    c = CachedArcade(fake, TTLCache(ttl=999))

    res = warm(c)
    assert res["categories"] == 2 and res["models"] == 1 and res["options"] == 2 and res["errors"] == 0

    before = dict(fake.calls)
    warm(c)                                   # second pass should hit the cache
    assert fake.calls == before


def test_cache_persists_to_disk(tmp_dir=None):
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "arcade.json"
        a = TTLCache(ttl=999, path=p)
        a.set("categories", [{"x": 1}])
        b = TTLCache(ttl=999, path=p)          # fresh instance reads the file
        assert b.get("categories") == [{"x": 1}]


# -- references (Instagram picture -> Arcade reference) --------------------

from app.media import detect_image_type, extract_og_image  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 16   # minimal byte pattern that sniffs as image/jpeg


def test_detect_image_type():
    assert detect_image_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert detect_image_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert detect_image_type(b"GIF89arest") == "image/gif"
    assert detect_image_type(b"RIFF\x00\x00\x00\x00WEBPrest") == "image/webp"
    assert detect_image_type(b"<html>nope") is None
    assert detect_image_type(b"") is None


def test_extract_og_image_handles_escapes_and_attr_order():
    a = '<meta property="og:image" content="https://cdn.x/y.jpg?a=1&amp;b=2" />'
    assert extract_og_image(a) == "https://cdn.x/y.jpg?a=1&b=2"
    b = '<meta content="https://cdn.x/i.png" property="og:image">'
    assert extract_og_image(b) == "https://cdn.x/i.png"
    assert extract_og_image("<html><body>nothing</body></html>") is None


def test_create_reference_pipeline(monkeypatch):
    calls = {"upload": 0, "put": 0, "create": 0}

    def fake_post(self, path, body):
        if path == "/references/createUpload":
            calls["upload"] += 1
            assert body["mediaType"] == "image/jpeg" and body["byteLength"] == len(JPEG)
            return {"uploadId": "abc.jpg", "uploadUrl": "https://up.example/x",
                    "method": "PUT", "requiredHeaders": {"Content-Type": "image/jpeg"}}
        if path == "/references/create":
            calls["create"] += 1
            return {"referenceId": "ref-1", "imageUrl": "u", "thumbnailUrl": "t",
                    "width": 10, "height": 10, "mediaType": "image/jpeg"}
        raise AssertionError("unexpected path " + path)

    def fake_request(method, url, content=None, headers=None, timeout=None):
        calls["put"] += 1
        assert method == "PUT" and headers.get("Content-Type") == "image/jpeg"
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(ArcadeClient, "_post", fake_post)
    monkeypatch.setattr("app.arcade.httpx.request", fake_request)

    ref = ArcadeClient(api_key="k").create_reference(JPEG)
    assert ref["referenceId"] == "ref-1"
    assert calls == {"upload": 1, "put": 1, "create": 1}


def test_create_reference_rejects_non_image():
    try:
        ArcadeClient(api_key="k").create_reference(b"definitely not an image")
    except ArcadeError as e:
        assert "unsupported" in str(e)
    else:
        raise AssertionError("expected ArcadeError")


def test_instagram_post_normalisation():
    """The URL rules the UI gates on, mirrored here for the backend contract."""
    import re
    def norm(s):
        s = s.strip()
        if not s:
            return None
        if not re.match(r"^https?://", s, re.I):
            s = "https://" + s
        from urllib.parse import urlparse
        u = urlparse(s)
        if u.netloc.replace("www.", "") != "instagram.com":
            return None
        m = re.match(r"^/(p|reel|reels|tv)/([A-Za-z0-9_-]+)", u.path)
        return m.group(2) if m else None
    assert norm("instagram.com/p/Cxyz123/?igsh=1") == "Cxyz123"
    assert norm("https://www.instagram.com/reel/AbC-9_/") == "AbC-9_"
    assert norm("example.com/p/abc") is None
    assert norm("instagram.com/instagram/") is None


# -- post -> reference cache ----------------------------------------------

from app.cache import PostReferenceCache, normalize_post_url  # noqa: E402


def test_normalize_post_url_collapses_tracking_and_case():
    a = normalize_post_url("https://www.instagram.com/p/Cxyz123/?igsh=abc")
    b = normalize_post_url("instagram.com/p/Cxyz123/")
    c = normalize_post_url("https://instagram.com/p/Cxyz123")
    assert a == b == c == "instagram.com/p/Cxyz123"     # shortcode case preserved


def test_post_reference_cache_roundtrip():
    c = PostReferenceCache(TTLCache(ttl=999))
    assert c.get("instagram.com/p/AAA/") is None
    c.set("instagram.com/p/AAA/", "ref-123")
    hit = c.get("https://www.instagram.com/p/AAA/?igsh=x")   # different spelling, same post
    assert hit == {"referenceId": "ref-123"}
    assert c.stats()["hits"] == 1 and c.stats()["misses"] == 1

    c.invalidate("instagram.com/p/AAA/")
    assert c.get("instagram.com/p/AAA/") is None


def test_with_scheme_accepts_bare_hosts():
    from app.media import _with_scheme
    assert _with_scheme("instagram.com/p/AbC/") == "https://instagram.com/p/AbC/"
    assert _with_scheme("https://instagram.com/p/AbC/") == "https://instagram.com/p/AbC/"
    assert _with_scheme("http://x/y") == "http://x/y"
    assert _with_scheme("") == ""


def test_reference_from_post_uses_cache(monkeypatch):
    """A repeat visit must not re-fetch Instagram or re-upload to Arcade."""
    import app.main as m

    fetches = {"ig": 0, "upload": 0, "get": 0}

    monkeypatch.setattr("app.media.fetch_instagram_post_image",
                        lambda url, timeout=25.0: (fetches.__setitem__("ig", fetches["ig"] + 1)
                                                   or (JPEG, "image/jpeg")))

    class FakeArcade:
        def create_reference(self, image, media_type=None, request_id=None):
            fetches["upload"] += 1
            return {"referenceId": "ref-9", "imageUrl": "u", "thumbnailUrl": "t"}

        def get_reference(self, rid):
            fetches["get"] += 1
            return {"referenceId": rid, "imageUrl": "u2", "thumbnailUrl": "t2"}

    monkeypatch.setattr(m, "bot", type("B", (), {"arcade": FakeArcade()})())
    monkeypatch.setattr(m, "_post_cache", PostReferenceCache(TTLCache(ttl=999)))

    first = m._reference_from_post("https://www.instagram.com/p/Post1/")
    second = m._reference_from_post("instagram.com/p/Post1/?igsh=zz")   # same post

    assert first["referenceId"] == second["referenceId"] == "ref-9"
    assert fetches["ig"] == 1        # Instagram hit once, not twice
    assert fetches["upload"] == 1    # Arcade upload once, not twice
    assert fetches["get"] == 1       # second call only renewed the preview


def test_reference_cache_rebuilds_when_stale(monkeypatch):
    import app.main as m

    fetches = {"ig": 0, "upload": 0}

    monkeypatch.setattr("app.media.fetch_instagram_post_image",
                        lambda url, timeout=25.0: (fetches.__setitem__("ig", fetches["ig"] + 1)
                                                   or (JPEG, "image/jpeg")))

    class FakeArcade:
        def create_reference(self, image, media_type=None, request_id=None):
            fetches["upload"] += 1
            return {"referenceId": "ref-new", "imageUrl": "u", "thumbnailUrl": "t"}

        def get_reference(self, rid):
            raise RuntimeError("404 NOT_FOUND")      # deleted upstream

    cache = PostReferenceCache(TTLCache(ttl=999))
    cache.set("instagram.com/p/Gone/", "ref-dead")
    monkeypatch.setattr(m, "bot", type("B", (), {"arcade": FakeArcade()})())
    monkeypatch.setattr(m, "_post_cache", cache)

    ref = m._reference_from_post("instagram.com/p/Gone/")
    assert ref["referenceId"] == "ref-new"           # fell back to a rebuild
    assert fetches == {"ig": 1, "upload": 1}
