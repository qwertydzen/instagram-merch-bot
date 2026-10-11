"""Fetch an image to use as an Arcade reference.

Arcade can generate a pattern FROM an image: upload it (references/createUpload
-> PUT -> references/create) and pass the resulting `referenceId` to
patterns/create as `referenceIds`.

Sources:
  * an Instagram post URL — we read the post's `og:image` and fetch those bytes.
    Instagram serves og tags to crawler user-agents; this is best-effort, since
    private posts and rate limits will fail (handled as a normal error).
  * any direct image URL.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Optional, Tuple

import httpx

log = logging.getLogger("media")

# Instagram/Facebook serve OpenGraph tags to this crawler UA.
CRAWLER_UA = (
    "Mozilla/5.0 (compatible; facebookexternalhit/1.1; "
    "+https://developers.facebook.com/docs/sharing/webmasters/crawler)"
)
MAX_BYTES = 10 * 1024 * 1024  # Arcade's reference limit is 10 MiB

_SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]

_OG_A = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_OG_B = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)


class ImageError(ValueError):
    pass


def detect_image_type(data: bytes) -> Optional[str]:
    """Arcade accepts image/gif|jpeg|png|webp. Sniff from the bytes."""
    if not data:
        return None
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def extract_og_image(page_html: str) -> Optional[str]:
    for pattern in (_OG_A, _OG_B):
        match = pattern.search(page_html or "")
        if match:
            return html.unescape(match.group(1))
    return None


def _with_scheme(url: str) -> str:
    """Accept 'instagram.com/p/x' as well as a full URL."""
    s = (url or "").strip()
    if s and not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    return s


def fetch_url_image(url: str, timeout: float = 25.0) -> Tuple[bytes, str]:
    """Download an image URL and return (bytes, media_type)."""
    url = _with_scheme(url)
    try:
        resp = httpx.get(url, headers={"User-Agent": CRAWLER_UA}, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ImageError(f"could not download image: {exc}") from exc
    if resp.status_code >= 400:
        raise ImageError(f"image download -> HTTP {resp.status_code}")
    data = resp.content
    media_type = detect_image_type(data)
    if not media_type:
        raise ImageError("not a supported image (png / jpeg / webp / gif)")
    if len(data) > MAX_BYTES:
        raise ImageError(f"image too large ({len(data)} bytes; max {MAX_BYTES})")
    return data, media_type


def fetch_instagram_post_image(post_url: str, timeout: float = 25.0) -> Tuple[bytes, str]:
    """Read the image of a public Instagram post via its og:image tag."""
    post_url = _with_scheme(post_url)
    try:
        page = httpx.get(post_url, headers={"User-Agent": CRAWLER_UA}, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ImageError(f"could not reach the post: {exc}") from exc
    if page.status_code >= 400:
        raise ImageError(f"post page -> HTTP {page.status_code}")
    og = extract_og_image(page.text)
    if not og:
        raise ImageError("no image found on that post (private, or Instagram served a login page)")
    return fetch_url_image(og, timeout=timeout)
