"""Instagram Messaging (Meta Graph API): DMs, comment private replies, webhooks.

Live mode posts to /{ig_user_id}/messages (or /replies for public comment
replies). Mock mode logs outbound so the whole flow runs with no Meta app.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings

log = logging.getLogger("instagram")


class InstagramError(RuntimeError):
    pass


class InstagramClient:
    def __init__(
        self,
        access_token: Optional[str] = None,
        ig_user_id: Optional[str] = None,
        graph_base_url: Optional[str] = None,
        api_version: Optional[str] = None,
        timeout: float = 20.0,
    ):
        self.access_token = access_token or Settings.IG_ACCESS_TOKEN
        self.ig_user_id = ig_user_id or Settings.IG_USER_ID
        self.graph_base = (graph_base_url or Settings.GRAPH_BASE_URL).rstrip("/")
        self.version = api_version or Settings.GRAPH_API_VERSION
        self.timeout = timeout

    def _base(self) -> str:
        return f"{self.graph_base}/{self.version}/{self.ig_user_id}"

    def _post(self, url: str, json: Dict[str, Any], extra_params: Optional[Dict[str, Any]] = None):
        params = {"access_token": self.access_token}
        if extra_params:
            params.update(extra_params)
        resp = httpx.post(url, params=params, json=json, timeout=self.timeout)
        if resp.status_code >= 400:
            raise InstagramError(f"POST {url} -> HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    # -- direct messages ---------------------------------------------------
    def _send_message(self, recipient: Dict[str, str], message: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"{self._base()}/messages", {"recipient": recipient, "message": message})

    def send_text(self, recipient_id: str, text: str) -> Dict[str, Any]:
        return self._send_message({"id": recipient_id}, {"text": text[:1000]})

    def send_image(self, recipient_id: str, image_url: str) -> Dict[str, Any]:
        return self._send_message(
            {"id": recipient_id},
            {"attachment": {"type": "image", "payload": {"url": image_url}}},
        )

    # -- comment private replies ------------------------------------------
    # A "private reply" DMs the author of a comment WITHOUT them messaging you
    # first. Allowed once per comment, within 7 days of it being posted.
    def send_private_reply_text(self, comment_id: str, text: str) -> Dict[str, Any]:
        return self._send_message({"comment_id": comment_id}, {"text": text[:1000]})

    def send_private_reply_image(self, comment_id: str, image_url: str) -> Dict[str, Any]:
        return self._send_message(
            {"comment_id": comment_id},
            {"attachment": {"type": "image", "payload": {"url": image_url}}},
        )

    # -- public comment reply (optional) ----------------------------------
    # POST /{ig-id}/replies?message={comment_id} with the reply text.
    def reply_to_comment(self, comment_id: str, text: str) -> Dict[str, Any]:
        return self._post(
            f"{self._base()}/replies",
            {"message": text[:2000]},
            extra_params={"message": comment_id},
        )


class MockInstagramClient:
    """Logs outbound messages instead of calling Meta."""

    def __init__(self, *_, **__):
        self.sent: List[Dict[str, Any]] = []

    def _record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        self.sent.append(rec)
        return {"message_id": "mock"}

    def send_text(self, recipient_id: str, text: str) -> Dict[str, Any]:
        log.info("[mock IG DM] -> %s: %s", recipient_id, text.replace("\n", " ")[:120])
        return self._record({"to": recipient_id, "via": "dm", "type": "text", "text": text})

    def send_image(self, recipient_id: str, image_url: str) -> Dict[str, Any]:
        log.info("[mock IG DM] -> %s: [image] %s", recipient_id, image_url)
        return self._record({"to": recipient_id, "via": "dm", "type": "image", "url": image_url})

    def send_private_reply_text(self, comment_id: str, text: str) -> Dict[str, Any]:
        log.info("[mock IG private-reply] comment %s: %s", comment_id, text.replace("\n", " ")[:120])
        return self._record({"to": comment_id, "via": "private_reply", "type": "text", "text": text})

    def send_private_reply_image(self, comment_id: str, image_url: str) -> Dict[str, Any]:
        log.info("[mock IG private-reply] comment %s: [image] %s", comment_id, image_url)
        return self._record({"to": comment_id, "via": "private_reply", "type": "image", "url": image_url})

    def reply_to_comment(self, comment_id: str, text: str) -> Dict[str, Any]:
        log.info("[mock IG public reply] -> comment %s: %s", comment_id, text[:120])
        return self._record({"to": comment_id, "via": "public_reply", "type": "text", "text": text})


# -- webhook helpers -------------------------------------------------------

def verify_signature(raw_body: bytes, signature_header: Optional[str], app_secret: str) -> bool:
    """Validate X-Hub-Signature-256 (sha256=<hex>) when an app secret is set."""
    if not app_secret:
        return True  # not configured -> skip (dev only)
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])


def extract_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pull (sender_id, text) pairs out of an Instagram messaging webhook body.

    Ignores echo events (the bot's own messages) and non-text attachments.
    """
    out: List[Dict[str, str]] = []
    for entry in payload.get("entry", []) or []:
        for event in entry.get("messaging", []) or []:
            if event.get("message", {}).get("is_echo"):
                continue
            sender = (event.get("sender") or {}).get("id")
            text = (event.get("message") or {}).get("text")
            if sender and text:
                out.append({"sender_id": sender, "text": text})
    return out


def extract_comments(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pull comment events out of an Instagram webhook (`field: "comments"`).

    Returns dicts: comment_id, commenter_id, username, text, media_id.
    """
    out: List[Dict[str, str]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            if change.get("field") != "comments":
                continue
            value = change.get("value") or {}
            author_id = (value.get("from") or {}).get("id")
            # ignore the account replying to its own comments
            if author_id and author_id == str(entry.get("id")):
                continue
            out.append(
                {
                    "comment_id": str(value.get("id", "")),
                    "commenter_id": str(author_id or ""),
                    "username": (value.get("from") or {}).get("username", ""),
                    "text": value.get("text", "") or "",
                    "media_id": str((value.get("media") or {}).get("id", "")),
                }
            )
    return out


def build_arcade_client():
    """Return a live or mock Arcade client based on settings."""
    if Settings.arcade_is_live():
        from .arcade import ArcadeClient

        return ArcadeClient()
    from .mock_arcade import MockArcadeClient

    log.warning("Arcade in MOCK mode (no ARCADE_API_KEY). Replies use fake products.")
    return MockArcadeClient()


def build_instagram_client():
    if Settings.ig_is_live():
        return InstagramClient()
    return MockInstagramClient()
