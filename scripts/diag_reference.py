#!/usr/bin/env python3
"""Does a reference actually change the generated pattern?

Builds a deliberately loud test image (magenta/cyan diagonals), registers it as
an Arcade reference, then generates the SAME prompt+model twice: once with the
reference, once without. Saves both pattern images so they can be compared.

    env -u PYTHONPATH .venv/bin/python scripts/diag_reference.py
"""
from __future__ import annotations

import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx                                   # noqa: E402
from app.arcade import ArcadeClient, ArcadeError   # noqa: E402

OUT = "/Users/user/.hermes/cache/scratch"
PROMPT = "botanical florals"


def make_png(width: int, height: int, fn) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)                          # filter: none
        for x in range(width):
            raw += bytes(fn(x, y))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def loud_image(w=512, h=512) -> bytes:
    """Magenta/cyan diagonal stripes with a black cross — impossible to miss."""
    def px(x, y):
        if abs(x - y) < 18 or abs((w - x) - y) < 18:
            return (0, 0, 0)
        return (255, 0, 255) if ((x + y) // 40) % 2 == 0 else (0, 255, 255)
    return make_png(w, h, px)


def save(url: str, path: str) -> int:
    r = httpx.get(url, timeout=40, follow_redirects=True)
    r.raise_for_status()
    with open(path, "wb") as fh:
        fh.write(r.content)
    return len(r.content)


def main() -> int:
    c = ArcadeClient()

    print("1) building + registering the test reference")
    png = loud_image()
    print(f"   test png: {len(png):,} bytes")
    ref = c.create_reference(png, "image/png")
    rid = ref["referenceId"]
    print(f"   referenceId {rid}  {ref.get('width')}x{ref.get('height')}")
    print(f"   stored at   {str(ref.get('imageUrl'))[:90]}")

    models = c.list_models()
    model_id = models[0]["modelId"] if models else None
    print(f"   using model: {models[0]['name'] if models else '(none)'}")

    print("\n2) pattern WITH the reference")
    job = c.wait_job(c.create_pattern(PROMPT, model_id=model_id, reference_ids=[rid])["jobId"])
    if job.get("status") != "succeeded" or not job.get("result"):
        print("   ✗", job.get("error")); return 1
    with_ref_pat = job["result"]["patternId"]

    print("3) pattern WITHOUT the reference (same prompt + model)")
    job2 = c.wait_job(c.create_pattern(PROMPT, model_id=model_id)["jobId"])
    if job2.get("status") != "succeeded" or not job2.get("result"):
        print("   ✗", job2.get("error")); return 1
    without_ref_pat = job2["result"]["patternId"]

    print("\n4) fetching both patterns")
    data = []
    for label, pid in (("with_ref", with_ref_pat), ("without_ref", without_ref_pat)):
        pat = c._post("/patterns/get", {"patternId": pid})
        url = pat.get("imageUrl")
        path = os.path.join(OUT, f"pattern_{label}.jpg")
        size = save(url, path)
        data.append((label, pid, size, path, pat.get("prompt")))
        print(f"   {label:12} {pid}  {size:,} bytes -> {path}")
        print(f"                prompt={pat.get('prompt')!r}")

    # byte-level comparison (crude but decisive when images are identical)
    a = open(data[0][3], "rb").read()
    b = open(data[1][3], "rb").read()
    print(f"\n5) identical bytes? {a == b}")
    print(f"   sizes: {len(a):,} vs {len(b):,}")
    print(f"\nREFERENCE png also saved for comparison:")
    ref_path = os.path.join(OUT, "reference_test.png")
    with open(ref_path, "wb") as fh:
        fh.write(png)
    print(f"   {ref_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
