"""Instagram merch bot — FastAPI app.

Routes:
  GET  /webhook   Meta webhook verification handshake
  POST /webhook   Incoming Instagram DM events -> build product -> reply
  POST /simulate  Local end-to-end test without a Meta app
  GET  /health    Liveness + which subsystems are live vs mocked
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import builder as builder_svc
from .bot import MerchBot
from .cache import CachedArcade, PostReferenceCache, TTLCache, warm
from .config import Settings
from .instagram import (
    build_arcade_client,
    build_instagram_client,
    extract_comments,
    extract_messages,
    verify_signature,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Instagram Merch Bot", version="0.1.0")
# Allow the in-chat / local tester to call the API from a different origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Arcade service -------------------------------------------------------
# Catalogue reads (categories, styles, option trees) go through a TTL cache
# (memory + disk). Work-creating calls pass straight through.
_cache: Optional[TTLCache] = None
_post_cache: Optional[PostReferenceCache] = None
_arcade = build_arcade_client()
if Settings.CACHE_ENABLED:
    _cache = TTLCache(ttl=Settings.CACHE_TTL_SECONDS, path=Path(Settings.CACHE_DIR) / "arcade.json")
    _arcade = CachedArcade(_arcade, _cache)
    _post_cache = PostReferenceCache(
        TTLCache(ttl=Settings.POST_CACHE_TTL_SECONDS, path=Path(Settings.CACHE_DIR) / "posts.json")
    )

bot = MerchBot(arcade=_arcade, instagram=build_instagram_client())

_CATALOG_HEADERS = {"Cache-Control": f"public, max-age={max(0, Settings.CACHE_TTL_SECONDS)}"}


@app.on_event("startup")
def _warm_cache() -> None:
    """Prefetch the catalogue in the background so the first open is instant."""
    if not (Settings.CACHE_ENABLED and Settings.WARM_CACHE_ON_START):
        return
    def _run():
        log.info("cache warm: %s", warm(_arcade))
    threading.Thread(target=_run, daemon=True).start()


class SimulateRequest(BaseModel):
    sender_id: str = "test-user"
    text: str


class SimulateCommentRequest(BaseModel):
    comment_id: str = "test-comment"
    text: str
    commenter_id: str = "test-commenter"
    username: str = "tester"


@app.get("/")
def index() -> Response:
    """Product builder (the mini-app)."""
    return FileResponse(STATIC_DIR / "builder.html")


@app.get("/builder")
def builder_page() -> Response:
    return FileResponse(STATIC_DIR / "builder.html")


@app.get("/chat")
def chat_page() -> Response:
    """Chat tester (DM / comment simulator)."""
    return FileResponse(STATIC_DIR / "chat.html")


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "mode": Settings.describe()}


@app.get("/webhook")
def verify_webhook(request: Request) -> Response:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and token == Settings.IG_VERIFY_TOKEN:
        log.info("webhook verified")
        return PlainTextResponse(challenge)
    log.warning("webhook verification failed (mode=%s)", mode)
    return PlainTextResponse("forbidden", status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(raw, signature, Settings.IG_APP_SECRET):
        log.warning("bad webhook signature")
        return JSONResponse({"error": "bad signature"}, status_code=403)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "ignored"}, status_code=200)

    messages: List[Dict[str, str]] = extract_messages(payload)
    for msg in messages:
        try:
            bot.handle_message(msg["sender_id"], msg["text"])
        except Exception:  # noqa: BLE001
            log.exception("failed handling message from %s", msg["sender_id"])

    comments: List[Dict[str, str]] = extract_comments(payload)
    for c in comments:
        try:
            bot.handle_comment(
                c["comment_id"], c["text"],
                commenter_id=c.get("commenter_id"), username=c.get("username"),
            )
        except Exception:  # noqa: BLE001
            log.exception("failed handling comment %s", c.get("comment_id"))

    # Meta expects a fast 200 regardless of downstream processing.
    return JSONResponse({"status": "ok"})


@app.post("/simulate")
def simulate(req: SimulateRequest) -> Dict[str, Any]:
    """Fire a fake DM at the bot and return what it replied with."""
    return bot.handle_message(req.sender_id, req.text)


@app.post("/simulate-comment")
def simulate_comment(req: SimulateCommentRequest) -> Dict[str, Any]:
    """Fire a fake post comment at the bot -> it sends a private-reply DM."""
    return bot.handle_comment(
        req.comment_id, req.text, commenter_id=req.commenter_id, username=req.username
    )


# --------------------------------------------------------------------------
# Product builder API (the mini-app)
# --------------------------------------------------------------------------

class OptionsRequest(BaseModel):
    categoryId: str
    selections: Dict[str, str] = {}


class GenerateRequest(BaseModel):
    categoryId: str
    prompt: str
    selections: Dict[str, str] = {}
    modelId: Optional[str] = None
    colors: List[str] = []
    quantity: int = 1
    postUrl: Optional[str] = None        # Instagram post the buyer came from
    referenceId: Optional[str] = None    # already-uploaded Arcade reference


class CheckoutRequest(BaseModel):
    productId: str
    quantity: int = 1


@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    return {"status": "ok", "mode": Settings.describe()}


@app.get("/api/categories")
def api_categories() -> Response:
    return JSONResponse({"items": bot.arcade.list_categories()}, headers=_CATALOG_HEADERS)


@app.get("/api/models")
def api_models() -> Response:
    return JSONResponse({"items": bot.arcade.list_models()}, headers=_CATALOG_HEADERS)


@app.post("/api/options")
def api_options(req: OptionsRequest) -> Dict[str, Any]:
    """categories/get — options + availability + a resolved full selection."""
    return builder_svc.get_options(bot.arcade, req.categoryId, req.selections)


@app.post("/api/generate")
def api_generate(req: GenerateRequest) -> Dict[str, Any]:
    reference_ids: List[str] = []
    if req.referenceId:
        reference_ids = [req.referenceId]
    elif req.postUrl:
        try:
            reference_ids = [_reference_from_post(req.postUrl)["referenceId"]]
        except Exception as exc:  # noqa: BLE001
            # never block the purchase because the post image couldn't be read
            log.warning("no reference from post %s: %s", req.postUrl, exc)
    try:
        return builder_svc.generate_product(
            bot.arcade,
            categoryId=req.categoryId,
            prompt=req.prompt,
            selections=req.selections,
            modelId=req.modelId,
            colors=req.colors,
            quantity=req.quantity,
            reference_ids=reference_ids,
        )
    except builder_svc.BuildError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class PostRefRequest(BaseModel):
    postUrl: str


def _reference_from_post(post_url: str) -> Dict[str, Any]:
    """Instagram post -> its picture -> an Arcade reference (cached per post).

    On a repeat visit we skip BOTH the Instagram fetch and the Arcade upload and
    reuse the stored referenceId, renewing only the short-lived preview URLs.
    """
    from .media import fetch_instagram_post_image

    if _post_cache:
        hit = _post_cache.get(post_url)
        if hit and hit.get("referenceId"):
            try:
                ref = bot.arcade.get_reference(hit["referenceId"])
                log.info("post cache HIT: %s -> %s", post_url, hit["referenceId"])
                return ref
            except Exception as exc:  # noqa: BLE001
                log.warning("cached reference %s unusable (%s) — rebuilding", hit["referenceId"], exc)
                _post_cache.invalidate(post_url)

    image, media_type = fetch_instagram_post_image(post_url)
    ref = bot.arcade.create_reference(image, media_type)
    if _post_cache and ref.get("referenceId"):
        _post_cache.set(post_url, ref["referenceId"])
    log.info("post cache MISS: %s -> %s", post_url, ref.get("referenceId"))
    return ref


@app.post("/api/reference/from-post")
def api_reference_from_post(req: PostRefRequest) -> Dict[str, Any]:
    """Read the post's image and register it as an Arcade reference.

    Returns the referenceId (pass it back as `referenceId` to /api/generate) plus
    a preview URL, so the UI can show which picture will guide the design.
    """
    try:
        ref = _reference_from_post(req.postUrl)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"could not read that post's image: {exc}")
    return {
        "referenceId": ref.get("referenceId"),
        "imageUrl": ref.get("imageUrl"),
        "thumbnailUrl": ref.get("thumbnailUrl"),
        "width": ref.get("width"),
        "height": ref.get("height"),
    }


@app.post("/api/checkout")
def api_checkout(req: CheckoutRequest) -> Dict[str, Any]:
    return builder_svc.create_checkout(bot.arcade, req.productId, req.quantity)


@app.get("/api/products")
def api_products() -> Dict[str, Any]:
    return {"items": bot.arcade.list_products()}


@app.get("/api/cache")
def api_cache() -> Dict[str, Any]:
    """Cache status: catalogue + per-post reference mapping."""
    return {
        "enabled": Settings.CACHE_ENABLED,
        "warm_on_start": Settings.WARM_CACHE_ON_START,
        "catalog": _cache.stats() if _cache else None,
        "posts": _post_cache.stats() if _post_cache else None,
    }


@app.post("/api/cache/refresh")
def api_cache_refresh(scope: str = "all") -> Dict[str, Any]:
    """Drop cached data. scope=catalog | posts | all."""
    if _cache is None and _post_cache is None:
        return {"ok": False, "reason": "cache disabled (CACHE_ENABLED=false)"}
    out: Dict[str, Any] = {"ok": True, "scope": scope}
    if scope in ("all", "catalog") and _cache is not None:
        out["catalog_dropped"] = _cache.invalidate()
        out["warm"] = warm(_arcade)
    if scope in ("all", "posts") and _post_cache is not None:
        out["posts_dropped"] = _post_cache._cache.invalidate("post|")  # noqa: SLF001
    out["catalog"] = _cache.stats() if _cache else None
    out["posts"] = _post_cache.stats() if _post_cache else None
    return out
