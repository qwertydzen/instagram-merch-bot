"""Orchestration.

Two entry points:
  handle_message(...)  — someone DMs the bot
  handle_comment(...)  — someone comments on a post; bot sends a private reply
                         (a DM to the commenter, no prior message needed)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Set

from .config import Settings
from .intent import parse_intent, pick_category
from .instagram import build_arcade_client, build_instagram_client

log = logging.getLogger("bot")

# filler words that don't count as a design brief
_STOPWORDS = {
    "a", "an", "the", "me", "my", "i", "for", "of", "to", "with", "and", "in",
    "on", "please", "it", "this", "that", "you", "your", "some", "one", "get",
    "give", "need", "would", "like", "can", "could", "hey", "hi", "hello",
}
ASK_TEXT = (
    "Hey! Happy to make you something 👋\n"
    "Reply here with a short brief — e.g. \"a canvas tote with desert cacti\" — "
    "and I'll send the design plus a one-tap buy link."
)


def format_caption(product: Dict[str, Any], quantity: int, checkout: Dict[str, Any]) -> str:
    title = product.get("title") or "your custom piece"
    label = product.get("selectionLabel") or ""
    price = product.get("unitPrice") or {}
    amount = price.get("amount")
    currency = price.get("currency", "")

    lines = [f"Here's your design: {title}"]
    if label:
        lines.append(f"Options: {label}")
    if amount:
        qty_note = f" x{quantity}" if quantity > 1 else ""
        lines.append(f"Price: {currency} {amount}{qty_note}")
    desc = (product.get("description") or "").strip()
    if desc:
        lines.append(desc[:280])
    lines.append("")
    lines.append("Order it here:")
    lines.append(checkout.get("checkoutUrl", "(no checkout url)"))
    return "\n".join(lines)


class MerchBot:
    def __init__(self, arcade=None, instagram=None):
        self.arcade = arcade or build_arcade_client()
        self.instagram = instagram or build_instagram_client()
        self._handled_comments: Set[str] = set()

    # -- entry points ------------------------------------------------------
    def handle_message(self, sender_id: str, text: str) -> Dict[str, Any]:
        if not text or not text.strip():
            return self._fail("dm", sender_id, "Tell me what you'd like made — e.g. \"a canvas tote with desert cacti\".")
        return self._run(text, send=lambda img, cap: self._send_dm(sender_id, img, cap),
                         context={"sender_id": sender_id, "via": "dm"})

    def handle_comment(
        self,
        comment_id: str,
        text: str,
        commenter_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Comment -> private-reply DM with product + buy link."""
        if not comment_id:
            return {"ok": False, "via": "comment", "error": "missing comment_id"}
        if comment_id in self._handled_comments:
            return {"ok": False, "via": "comment", "comment_id": comment_id, "skipped": "already handled"}
        decision = self._classify_comment(text)
        if decision == "ignore":
            return {"ok": False, "via": "comment", "comment_id": comment_id, "skipped": "no trigger keyword"}

        self._handled_comments.add(comment_id)

        if decision == "ask":
            # comment was just "link please" with no brief — don't invent a product
            self.instagram.send_private_reply_text(comment_id, ASK_TEXT)
            if Settings.PUBLIC_REPLY:
                try:
                    self.instagram.reply_to_comment(comment_id, Settings.PUBLIC_REPLY_TEXT)
                except Exception:  # noqa: BLE001
                    log.exception("public reply failed")
            return {"ok": True, "via": "comment", "comment_id": comment_id, "asked_for_brief": True}

        return self._run(
            text,
            send=lambda img, cap: self._send_private_reply(comment_id, img, cap),
            context={"comment_id": comment_id, "commenter_id": commenter_id, "username": username, "via": "comment"},
            public_reply_to=comment_id,
        )

    # -- shared pipeline ---------------------------------------------------
    def _run(self, text: str, send, context: Dict[str, Any], public_reply_to: Optional[str] = None) -> Dict[str, Any]:
        intent = parse_intent(text)
        log.info("build prompt=%r hint=%s qty=%s via=%s", intent.prompt, intent.category_hint, intent.quantity, context.get("via"))

        try:
            categories = self.arcade.list_categories()
        except Exception as exc:  # noqa: BLE001
            log.exception("categories/list failed")
            return self._fail(context.get("via", "dm"), context, f"I couldn't reach the merch engine: {exc}")

        category = pick_category(categories, intent.category_hint)
        if category is None:
            return self._fail(context.get("via", "dm"), context, "No product categories are available right now.")

        try:
            built = self.arcade.build_product_from_prompt(intent.prompt, category["categoryId"], intent.quantity)
        except Exception as exc:  # noqa: BLE001
            log.exception("product build failed")
            return self._fail(context.get("via", "dm"), context, f"Sorry, the design job failed: {exc}")

        product, checkout = built["product"], built["checkout"]
        images = product.get("images") or []
        image_url = images[0]["url"] if images else product.get("imageUrl")
        caption = format_caption(product, intent.quantity, checkout)

        send(image_url, caption)

        if public_reply_to and Settings.PUBLIC_REPLY:
            try:
                self.instagram.reply_to_comment(public_reply_to, Settings.PUBLIC_REPLY_TEXT)
            except Exception:  # noqa: BLE001
                log.exception("public reply failed")

        return {
            "ok": True,
            "via": context.get("via", "dm"),
            **{k: v for k, v in context.items() if k != "via"},
            "intent": {
                "prompt": intent.prompt,
                "category_hint": intent.category_hint,
                "quantity": intent.quantity,
                "category": category.get("name"),
            },
            "product": product,
            "checkout": checkout,
            "caption": caption,
        }

    # -- delivery ----------------------------------------------------------
    def _send_dm(self, recipient_id: str, image_url: Optional[str], caption: str) -> None:
        self.instagram.send_text(recipient_id, "On it — designing that now. This takes a minute…")
        if image_url:
            self.instagram.send_image(recipient_id, image_url)
        self.instagram.send_text(recipient_id, caption)

    def _send_private_reply(self, comment_id: str, image_url: Optional[str], caption: str) -> None:
        # Private replies are limited, so skip the "thinking" ack and send the
        # product straight away.
        if image_url:
            self.instagram.send_private_reply_image(comment_id, image_url)
        self.instagram.send_private_reply_text(comment_id, caption)

    # -- helpers -----------------------------------------------------------
    def _classify_comment(self, text: str) -> str:
        """Return 'ignore' | 'ask' | 'build' for a comment.

        ignore: no trigger keyword and no category hint.
        ask:    triggered, but there's no design brief (e.g. just "link please").
        build:  triggered with real design content.
        """
        t = (text or "").strip()
        if not t:
            return "ignore"
        lowered = t.lower()
        intent = parse_intent(t)
        triggered = (
            any(tr in lowered for tr in Settings.COMMENT_TRIGGERS)
            or intent.category_hint is not None
        )
        if not triggered:
            return "ignore"
        words = re.findall(r"[a-z0-9']{2,}", lowered)
        content = [w for w in words if w not in _STOPWORDS and w not in Settings.COMMENT_TRIGGERS]
        return "build" if content else "ask"

    def _fail(self, via: str, context: Any, message: str) -> Dict[str, Any]:
        # context may be a plain recipient id (dm) or a dict
        if isinstance(context, str):
            self.instagram.send_text(context, message)
            return {"ok": False, "via": via, "error": message}
        cid = context.get("comment_id")
        if cid:
            try:
                self.instagram.send_private_reply_text(cid, message)
            except Exception:  # noqa: BLE001
                log.exception("failed to send failure private reply")
        elif context.get("sender_id"):
            self.instagram.send_text(context["sender_id"], message)
        return {"ok": False, "via": via, **context, "error": message}
