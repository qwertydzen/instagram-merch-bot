#!/usr/bin/env python3
"""Live smoke test against the REAL Arcade API (platform.arcade.ai).

Run this with your key in .env to see exactly which call fails and why:

    cd ~/instagram-merch-bot
    env -u PYTHONPATH .venv/bin/python scripts/smoke.py

It walks categories -> options -> generate -> checkout, printing the outcome of
each step, and maps HTTP failures to the cause (401 bad key, 403 edge/User-Agent,
409 idempotency conflict, 412 drop-ship precondition, 422 validation, 429 rate limit).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import builder                      # noqa: E402
from app.arcade import ArcadeClient, ArcadeError  # noqa: E402
from app.config import Settings              # noqa: E402

HINTS = {
    401: "key missing / invalid / revoked — mint a new one in Arcade Studio → Settings → API keys.",
    403: "edge rejection: missing or library-default User-Agent, or the key lacks access.",
    409: "requestId conflict: the same id was reused with a different body.",
    412: "PRECONDITION_FAILED: a quantity-1 line needs dropShip=true (no active subscription).",
    422: "INPUT_VALIDATION_FAILED: check data.fieldErrors in the response body.",
    429: "rate limited: wait the Retry-After seconds, then retry with the same requestId.",
}


def main() -> int:
    if not Settings.ARCADE_API_KEY:
        print("✗ ARCADE_API_KEY is empty — put your key in .env first.")
        return 2

    print(f"base URL : {Settings.ARCADE_BASE_URL}")
    print(f"user-agent: {Settings.ARCADE_USER_AGENT}")
    print(f"key      : …{Settings.ARCADE_API_KEY[-4:]}  ({len(Settings.ARCADE_API_KEY)} chars)\n")

    client = ArcadeClient()
    try:
        cats = client.list_categories()
        print(f"✓ categories/list   {len(cats)} categories")
        for c in cats[:6]:
            print(f"    · {c['name']}  [{c['world']}]")
        if not cats:
            print("! no categories — nothing to build")
            return 0

        models = client.list_models()
        print(f"✓ models/list       {len(models)} models")

        cat = next((c for c in cats if c["world"] == "print"), cats[0])
        opts = client.get_category(cat["categoryId"], {})
        keys = [o["key"] for o in opts.get("options", [])]
        print(f"✓ categories/get    {cat['name']}: options={keys} "
              f"canManufacture={opts.get('canManufacture')}")

        print(f"\n… patterns/create + build for {cat['name']} (this takes a minute or two)")
        out = builder.generate_product(
            client, cat["categoryId"], "a simple geometric pattern", selections={}
        )
        p = out["product"]
        print(f"✓ product           {p['productId']}")
        print(f"    title {p.get('title')!r}  label {p.get('selectionLabel')!r}")
        print(f"    price {p.get('unitPrice')}")

        co = builder.create_checkout(client, p["productId"], 1)
        print(f"✓ checkouts/create  {co['checkoutId']}")
        print(f"    {co['checkoutUrl']}")

        print("\nALL GOOD — the live API path works end to end.")
        return 0

    except ArcadeError as e:
        print(f"\n✗ FAILED  status={e.status}  code={e.code}")
        print(f"    {e}")
        if e.status in HINTS:
            print(f"    hint: {HINTS[e.status]}")
        if e.body:
            print(f"    body: {e.body}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ UNEXPECTED {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
