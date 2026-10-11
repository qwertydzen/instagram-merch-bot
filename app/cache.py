"""TTL cache for Arcade's read-only catalogue data.

The webview asks for categories, styles and option trees every time it opens,
but those change rarely. Cache them in memory and on disk so a cold app start is
instant and costs zero API calls.

Only READ-ONLY calls are cached. Anything that starts work — patterns/create,
products/create, checkouts/create, jobs/get — always goes to the API, because
those have side effects and short-lived results (image URLs expire).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("cache")


def stable_key(*parts: Any) -> str:
    """Deterministic cache key for arbitrary args (dicts get sorted)."""
    def enc(p: Any) -> str:
        if isinstance(p, (dict, list)):
            return json.dumps(p, sort_keys=True, separators=(",", ":"))
        return str(p)
    return "|".join(enc(p) for p in parts)


class TTLCache:
    """Thread-safe TTL cache with optional JSON persistence."""

    def __init__(self, ttl: float = 43200, path: Optional[Path] = None, clock: Callable[[], float] = time.time):
        self.ttl = float(ttl)
        self.clock = clock
        self.path = Path(path) if path else None
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load()

    # -- api ---------------------------------------------------------------
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            if entry.get("expires", 0) <= self.clock():
                self._data.pop(key, None)
                return None
            return entry.get("value")

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = {"value": value, "expires": self.clock() + self.ttl}
        self._save()

    def get_or_set(self, key: str, fn: Callable[[], Any]) -> Any:
        hit = self.get(key)
        if hit is not None:
            return hit
        value = fn()
        if value is not None:
            self.set(key, value)
        return value

    def invalidate(self, prefix: str = "") -> int:
        with self._lock:
            victims = [k for k in self._data if k.startswith(prefix)]
            for k in victims:
                self._data.pop(k, None)
        self._save()
        return len(victims)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            now = self.clock()
            live = sum(1 for e in self._data.values() if e.get("expires", 0) > now)
            return {
                "entries": len(self._data),
                "live": live,
                "ttl_seconds": self.ttl,
                "path": str(self.path) if self.path else None,
            }

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
            self._data = payload.get("entries", {}) or {}
            log.info("cache loaded: %s entries from %s", len(self._data), self.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache load failed (%s) — starting empty", exc)

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"entries": self._data}))
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache save failed: %s", exc)


class CachedArcade:
    """Read-through cache in front of an Arcade client.

    Memoises only the catalogue reads; every other attribute (including all
    work-creating calls) is passed straight through to the wrapped client.
    """

    READ_CALLS = ("list_categories", "list_models", "get_category")

    def __init__(self, inner: Any, cache: TTLCache):
        self._inner = inner
        self._cache = cache

    def list_categories(self):
        return self._cache.get_or_set("categories", self._inner.list_categories)

    def list_models(self):
        return self._cache.get_or_set("models", self._inner.list_models)

    def get_category(self, category_id: str, selections: Optional[Dict[str, str]] = None):
        selections = selections or {}
        key = stable_key("options", category_id, selections)
        return self._cache.get_or_set(key, lambda: self._inner.get_category(category_id, selections))

    def __getattr__(self, name: str):
        # anything not explicitly cached: jobs, patterns/create, checkouts, …
        return getattr(self._inner, name)

    @property
    def cache(self) -> TTLCache:
        return self._cache

    @property
    def inner(self) -> Any:
        return self._inner


def warm(arcade: Any, categories: Optional[list] = None) -> Dict[str, int]:
    """Prefetch the whole read-only catalogue into the cache.

    categories -> styles -> the default option set for every category. Safe to
    call repeatedly; a warm cache makes it nearly free.
    """
    result = {"categories": 0, "models": 0, "options": 0, "errors": 0}
    try:
        cats = arcade.list_categories()
        result["categories"] = len(cats)
    except Exception as exc:  # noqa: BLE001
        log.warning("warm: categories failed: %s", exc)
        result["errors"] += 1
        return result

    try:
        result["models"] = len(arcade.list_models())
    except Exception as exc:  # noqa: BLE001
        log.warning("warm: models failed: %s", exc)
        result["errors"] += 1

    for cat in cats:
        cid = cat.get("categoryId")
        if not cid:
            continue
        try:
            arcade.get_category(cid, {})
            result["options"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("warm: options for %s failed: %s", cat.get("name"), exc)
            result["errors"] += 1
    return result


def normalize_post_url(url: str) -> str:
    """Stable identity for an Instagram post: host + path, no scheme/tracking.

    Shortcodes are case-sensitive, so only the host is lowercased. `?igsh=…`
    and trailing slashes collapse to the same key.
    """
    s = (url or "").strip()
    if not s:
        return ""
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    from urllib.parse import urlparse

    u = urlparse(s)
    host = u.netloc.replace("www.", "").lower()
    return f"{host}{u.path.rstrip('/')}"


class PostReferenceCache:
    """Instagram post URL -> Arcade referenceId.

    Fetching the post image and uploading it to Arcade are the two slow parts
    of a reference; both are avoided on a repeat visit. The referenceId itself
    is durable, so this cache is safe with a long TTL. Short-lived preview URLs
    are NOT stored — callers renew them with references/get.
    """

    def __init__(self, cache: TTLCache):
        self._cache = cache
        self.hits = 0
        self.misses = 0
        self._lock = threading.RLock()

    def _key(self, post_url: str) -> str:
        return stable_key("post", normalize_post_url(post_url))

    def get(self, post_url: str) -> Optional[Dict[str, Any]]:
        entry = self._cache.get(self._key(post_url))
        with self._lock:
            if entry:
                self.hits += 1
            else:
                self.misses += 1
        return entry

    def set(self, post_url: str, reference_id: str) -> None:
        self._cache.set(self._key(post_url), {"referenceId": reference_id})

    def invalidate(self, post_url: str) -> None:
        self._cache.invalidate(self._key(post_url))

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, **self._cache.stats()}
