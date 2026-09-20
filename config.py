#!/usr/bin/env python3
"""Environment-based configuration for the Ron Telegram bot.

Copy `.env.example` to `.env` and fill in the values, or export them in
your shell. A `.env` file next to this module is loaded automatically;
variables already present in the environment take precedence.

Stdlib only — no third-party dependencies.
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv():
    """Load KEY=VALUE lines from .env (if present) into os.environ."""
    try:
        text = (BASE_DIR / ".env").read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"or export {name} in your shell."
        )
    return value


def _optional(name, default=None):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _optional_int(name):
    value = _optional(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(
            f"{name} must be an integer Telegram id, got {value!r}."
        )


# --- Required -------------------------------------------------------------
BOT_TOKEN = _required("BOT_TOKEN")        # from @BotFather
GROQ_API_KEY = _required("GROQ_API_KEY")  # console.groq.com

# --- Optional -------------------------------------------------------------
GROQ_MODEL = _optional("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_BASE_URL = _optional("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
BOT_USERNAME = _optional("BOT_USERNAME", "ronrebootedbot")
OWNER_ID = _optional_int("OWNER_ID")      # reserved for owner-only features
SYSTEM_PROMPT = _optional("SYSTEM_PROMPT")


def read_json_response(resp):
    """Parse a urllib HTTP response body as JSON."""
    return json.loads(resp.read().decode("utf-8"))


def telegram_api_base():
    """Bot API base URL. The token goes in literally, unquoted."""
    return f"https://api.telegram.org/bot{BOT_TOKEN}"
