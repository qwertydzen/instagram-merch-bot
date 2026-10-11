#!/usr/bin/env python3
"""End-to-end: Instagram post image -> Arcade reference -> product built from it.

    env -u PYTHONPATH .venv/bin/python scripts/test_reference.py [instagram post url]

Default URL is a public Instagram page (proves the og:image fetch path). Pass a
post URL (/p/, /reel/) to test the real thing.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import builder                       # noqa: E402
from app.arcade import ArcadeClient, ArcadeError  # noqa: E402
from app.config import Settings               # noqa: E402
from app.media import fetch_instagram_post_image  # noqa: E402

DEFAULT_URL = "https://www.instagram.com/instagram/"
PROMPT = "botanical florals"


def main() -> int:
    if not Settings.ARCADE_API_KEY:
        print("✗ ARCADE_API_KEY not set")
        return 2

    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    client = ArcadeClient()

    print(f"1) fetching image from {url}")
    try:
        image, media_type = fetch_instagram_post_image(url)
    except Exception as exc:  # noqa: BLE001
        print(f"   ✗ {exc}")
        return 1
    print(f"   ✓ {len(image):,} bytes, {media_type}")

    print("2) registering it as an Arcade reference")
    try:
        ref = client.create_reference(image, media_type)
    except ArcadeError as exc:
        print(f"   ✗ {exc}")
        return 1
    print(f"   ✓ referenceId {ref['referenceId']}  {ref.get('width')}x{ref.get('height')}")
    print(f"     preview {str(ref.get('imageUrl'))[:80]}")

    cats = client.list_categories()
    cat = next((c for c in cats if c["world"] == "print"), cats[0])
    print(f"3) generating a {cat['name']} FROM the reference (takes a minute or two)")
    t0 = time.time()
    try:
        out = builder.generate_product(
            client, cat["categoryId"], PROMPT, reference_ids=[ref["referenceId"]]
        )
    except ArcadeError as exc:
        print(f"   ✗ {exc}")
        return 1
    p = out["product"]
    print(f"   ✓ {p['productId']}")
    print(f"     title {p.get('title')!r}  price {p.get('unitPrice')}")
    print(f"     built in {time.time()-t0:.0f}s")

    co = builder.create_checkout(client, p["productId"], 1)
    print(f"4) checkout {co['checkoutUrl']}")
    print("\nOK — the post's picture drove the design.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
