"""Turn a DM into a structured request: a prompt + a category hint.

Kept deliberately simple (keyword routing) so it's predictable and testable.
Swap in an LLM call later if you want richer parsing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# keyword -> words that hint at a category name
_CATEGORY_HINTS = {
    "tote": ["tote", "bag", "handbag", "purse"],
    "shirt": ["tee", "t-shirt", "tshirt", "shirt", "apparel", "hoodie"],
    "mug": ["mug", "cup", "tumbler"],
    "poster": ["poster", "print", "wall art", "art print"],
    "napkin": ["napkin", "linen", "table", "placemat"],
    "pillow": ["pillow", "cushion"],
    "apron": ["apron"],
    "ring": ["ring", "jewelry", "jewellery", "necklace", "pendant"],
}

_QTY_PATTERNS = [
    re.compile(r"\bx\s*(\d{1,3})\b"),                       # x2 / x 2
    re.compile(r"\b(\d{1,3})\s*x\b"),                       # 2x
    re.compile(r"\bq(?:ty|uantity)\.?\s*(\d{1,3})\b"),      # qty 2 / quantity 2
    re.compile(r"\b(\d{1,3})\s*(?:of them|pieces|units|pcs)\b"),  # 2 pieces
]


@dataclass
class Intent:
    prompt: str
    category_hint: Optional[str] = None
    quantity: int = 1
    raw: str = ""
    tokens: List[str] = field(default_factory=list)


def parse_intent(text: str, default_quantity: int = 1) -> Intent:
    raw = (text or "").strip()
    lowered = raw.lower()
    tokens = re.findall(r"[a-z0-9'-]+", lowered)

    hint: Optional[str] = None
    for key, words in _CATEGORY_HINTS.items():
        if any(w in lowered for w in words):
            hint = key
            break

    quantity = default_quantity
    for pattern in _QTY_PATTERNS:
        match = pattern.search(lowered)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 999:
                quantity = value
                break

    prompt = _clean_prompt(raw, hint)
    return Intent(prompt=prompt, category_hint=hint, quantity=quantity, raw=raw, tokens=tokens)


def _clean_prompt(raw: str, hint: Optional[str]) -> str:
    """Strip imperative boilerplate and quantity markers so the prompt reads
    like a design brief."""
    text = raw.strip()
    text = re.sub(
        r"^(?:please\s+|can you\s+|could you\s+|i(?:'d| would)?\s*(?:like|want)\s+|"
        r"make me\s+|make\s+|create\s+|generate\s+|design\s+|give me\s+)+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # drop quantity markers: x2, 2x, qty 2, 2 pieces, 2 of them
    text = re.sub(
        r"\b(?:x\s*\d{1,3}|\d{1,3}\s*x|q(?:ty|uantity)\.?\s*\d{1,3}|"
        r"\d{1,3}\s*(?:of them|pieces|units|pcs))\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # strip leading request keywords/separators ("link please — a tote", "dm: ...")
    text = re.sub(
        r"^(?:(?:link|buy|shop|order|dm|send|please|hey|hi|hello)\b[\s,:\-–—]*)+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^[\s\-–—:,!?.]+", "", text)
    # strip trailing filler ("…thanks", "…please")
    text = re.sub(r"\b(?:please|thanks|thank you|now)\b[\s.!?]*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.!?")
    return text or raw or "abstract pattern"


def score_category(category_name: str, hint: Optional[str]) -> int:
    """Higher = better match of an Arcade category name against the hint."""
    if not hint:
        return 0
    name = (category_name or "").lower()
    words = _CATEGORY_HINTS.get(hint, [])
    score = 0
    for w in words:
        if w in name:
            score += 10
    # keep jewelry out of print requests and vice versa unless explicitly asked
    if hint == "ring" and "ring" in name:
        score += 20
    return score


def pick_category(categories: List[Dict[str, Any]], hint: Optional[str]) -> Optional[Dict[str, Any]]:
    """Choose the best Arcade category for a hint; fall back to first print one."""
    if not categories:
        return None
    if hint:
        ranked = sorted(categories, key=lambda c: score_category(c.get("name", ""), hint), reverse=True)
        if ranked and score_category(ranked[0].get("name", ""), hint) > 0:
            return ranked[0]
    for c in categories:
        if c.get("world") == "print":
            return c
    return categories[0]
