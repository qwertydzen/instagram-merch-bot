"""Runtime settings, loaded from .env (falls back to real environment)."""
from __future__ import annotations

import os
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:  # dotenv optional
        pass


_load_env()


def _truthy(name: str, default: str = "") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # Arcade
    ARCADE_API_KEY = os.getenv("ARCADE_API_KEY", "").strip()
    ARCADE_BASE_URL = os.getenv("ARCADE_BASE_URL", "https://platform.arcade.ai/v1").rstrip("/")
    ARCADE_USER_AGENT = os.getenv(
        "ARCADE_USER_AGENT", "instagram-merch-bot/1.0 (you@example.com)"
    ).strip()

    # Instagram / Meta Graph
    IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()
    IG_USER_ID = os.getenv("IG_USER_ID", "").strip()
    IG_VERIFY_TOKEN = os.getenv("IG_VERIFY_TOKEN", "change-me-verify-token")
    IG_APP_SECRET = os.getenv("IG_APP_SECRET", "").strip()
    GRAPH_BASE_URL = os.getenv("GRAPH_BASE_URL", "https://graph.instagram.com").rstrip("/")
    GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")

    # Runtime
    JOB_POLL_INTERVAL = float(os.getenv("JOB_POLL_INTERVAL", "3"))
    JOB_TIMEOUT = float(os.getenv("JOB_TIMEOUT", "180"))
    MOCK_MODE = _truthy("MOCK_MODE")

    # Cache — read-only catalogue data (categories, styles, option trees).
    # Warmed at startup and persisted to disk so a cold open costs 0 API calls.
    CACHE_ENABLED = _truthy("CACHE_ENABLED", "true")
    CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "43200"))  # 12h
    CACHE_DIR = os.getenv("CACHE_DIR", str(Path(__file__).resolve().parent.parent / "cache"))
    WARM_CACHE_ON_START = _truthy("WARM_CACHE_ON_START", "true")
    # Instagram post URL -> Arcade referenceId (avoids re-fetching + re-uploading).
    POST_CACHE_TTL_SECONDS = int(os.getenv("POST_CACHE_TTL_SECONDS", str(30 * 24 * 3600)))

    # Comment-to-DM triggers: a comment fires a DM if it contains any of these
    # words (case-insensitive).
    COMMENT_TRIGGERS = [
        t.strip().lower()
        for t in os.getenv(
            "COMMENT_TRIGGERS", "link,buy,shop,order,want,make,send,please,dm,🙋"
        ).split(",")
        if t.strip()
    ]
    # Also DM a public "check your DMs" reply on the original comment.
    PUBLIC_REPLY = _truthy("PUBLIC_REPLY")
    PUBLIC_REPLY_TEXT = os.getenv(
        "PUBLIC_REPLY_TEXT", "Sent you a DM with the design + buy link! 📩"
    )

    @classmethod
    def arcade_is_live(cls) -> bool:
        return bool(cls.ARCADE_API_KEY) and not cls.MOCK_MODE

    @classmethod
    def ig_is_live(cls) -> bool:
        return bool(cls.IG_ACCESS_TOKEN and cls.IG_USER_ID) and not cls.MOCK_MODE

    @classmethod
    def describe(cls) -> str:
        return (
            f"arcade={'live' if cls.arcade_is_live() else 'mock'} "
            f"instagram={'live' if cls.ig_is_live() else 'mock'} "
            f"verify_token_set={bool(cls.IG_VERIFY_TOKEN)}"
        )
