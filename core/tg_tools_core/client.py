"""Telegram client construction and the interactive login primitives.

This module is tenancy-agnostic: it never persists anything. A `LoginSession`
drives Telethon through the phone/code/2FA steps and, on success, hands back a
session string. Each app decides what to do with that string (the self-hosted
app encrypts and stores it; a public app might keep it only in memory).
"""
from __future__ import annotations

import re

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhonePasswordFloodError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from .exceptions import LoginError
from .models import UserBrief


_PHONE_RE = re.compile(r"^\+?\d{7,15}$")


def normalize_phone(phone: str) -> str:
    """Strip common separators from a phone number."""
    return re.sub(r"[\s\-().]", "", (phone or "").strip())


def _humanize(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not h:
        parts.append(f"{s}s")
    return " ".join(parts) or "a few seconds"


def build_client(session_string: str | None, api_id: int, api_hash: str) -> TelegramClient:
    """Construct (but do not connect) a Telethon client from a session string."""
    return TelegramClient(StringSession(session_string or None), api_id, api_hash)


def user_brief(entity, is_owner: bool = False) -> UserBrief:
    """Build a UserBrief from a Telethon user/chat entity."""
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    title = getattr(entity, "title", None)
    name = f"{first} {last}".strip() or (title or "")
    return UserBrief(
        id=getattr(entity, "id", 0),
        username=getattr(entity, "username", None),
        name=name,
        is_owner=is_owner,
    )


class LoginSession:
    """Drives a single interactive login on a fresh, in-memory client."""

    def __init__(self, api_id: int, api_hash: str) -> None:
        self._client = build_client(None, api_id, api_hash)
        self._phone: str | None = None

    @property
    def client(self) -> TelegramClient:
        return self._client

    async def send_code(self, phone: str) -> None:
        phone = normalize_phone(phone)
        if not _PHONE_RE.match(phone):
            raise LoginError(
                "Enter a valid phone number in international format, e.g. "
                "+15551234567 (digits only, with an optional leading +)."
            )
        await self._client.connect()
        self._phone = phone
        try:
            await self._client.send_code_request(phone)
        except FloodWaitError as exc:
            raise LoginError(
                f"Telegram is rate-limiting login-code requests. Wait about "
                f"{_humanize(exc.seconds)} before trying again — and don't retry "
                "until then, as each attempt resets the timer."
            ) from exc
        except PhonePasswordFloodError as exc:
            raise LoginError(
                "Telegram has temporarily blocked new login attempts for this "
                "number after too many tries. It doesn't report a fixed wait; this "
                "usually clears within a few hours (occasionally up to 24h). Stop "
                "retrying (further attempts can extend the block) — you can keep "
                "using the official Telegram app meanwhile."
            ) from exc
        except Exception as exc:  # network / invalid phone / other
            raise LoginError(f"Could not send code: {exc}") from exc

    async def sign_in_code(self, code: str) -> bool:
        """Returns True if a 2FA password is still required, else False (done)."""
        if self._phone is None:
            raise LoginError("No login in progress. Request a code first.")
        try:
            await self._client.sign_in(self._phone, code=code)
            return False
        except SessionPasswordNeededError:
            return True
        except PhoneCodeInvalidError as exc:
            raise LoginError("Invalid code.") from exc
        except PhoneCodeExpiredError as exc:
            raise LoginError("Code expired. Request a new one.") from exc

    async def sign_in_password(self, password: str) -> None:
        try:
            await self._client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise LoginError(
                f"Invalid 2FA password (received {len(password)} characters). "
                "Check for browser autofill, caps lock, keyboard layout, or a "
                "trailing space — use 'Show password' to verify what was entered."
            ) from exc

    def session_string(self) -> str:
        return self._client.session.save()

    async def me(self) -> UserBrief:
        return user_brief(await self._client.get_me())
