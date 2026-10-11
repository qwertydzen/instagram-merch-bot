#!/usr/bin/env python3
"""Pre-build the Arcade catalogue cache so the app never fetches on first open.

    cd ~/instagram-merch-bot
    env -u PYTHONPATH .venv/bin/python scripts/precache.py

Prefetches categories, styles and every category's default option tree into
cache/arcade.json. Run it after editing .env or when Arcade's catalogue changes;
the app also warms itself at startup.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cache import CachedArcade, TTLCache, warm   # noqa: E402
from app.config import Settings                      # noqa: E402
from app.instagram import build_arcade_client        # noqa: E402


def main() -> int:
    if not Settings.CACHE_ENABLED:
        print("✗ CACHE_ENABLED is false — nothing to pre-build.")
        return 2

    path = Path(Settings.CACHE_DIR) / "arcade.json"
    cache = TTLCache(ttl=Settings.CACHE_TTL_SECONDS, path=path)
    arcade = CachedArcade(build_arcade_client(), cache)

    print(f"mode      : {'live' if Settings.arcade_is_live() else 'mock'}")
    print(f"cache file: {path}")
    print(f"ttl       : {Settings.CACHE_TTL_SECONDS}s\n")

    t0 = time.time()
    try:
        result = warm(arcade)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ warm failed: {exc}")
        return 1

    took = time.time() - t0
    print(f"✓ categories {result['categories']}")
    print(f"✓ styles     {result['models']}")
    print(f"✓ option sets{result['options']:>4}   (errors: {result['errors']})")
    print(f"\nwarmed in {took:.1f}s")
    print(f"cache stats: {cache.stats()}")

    size = path.stat().st_size if path.exists() else 0
    print(f"on disk    : {size/1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
