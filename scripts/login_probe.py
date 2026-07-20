#!/usr/bin/env python3
"""Standalone Telethon login probe — bypasses the app to isolate auth issues.

Logs in with raw Telethon using your TG_API_ID / TG_API_HASH from the repo-root
.env (or the environment). Persists nothing (in-memory session). Use this to
determine whether a 2FA rejection comes from our app or from Telethon/account.

Run with the venv active, from the repo root:
    python scripts/login_probe.py
"""
from __future__ import annotations

import asyncio
import getpass
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession


def load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


async def main() -> None:
    load_env()
    try:
        api_id = int(os.environ["TG_API_ID"])
        api_hash = os.environ["TG_API_HASH"]
    except KeyError as exc:
        raise SystemExit(f"Missing {exc} — set it in .env or the environment.")

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    print("connected to Telegram.")

    phone = input("Phone (e.g. +15551234567): ").strip()
    await client.send_code_request(phone)
    code = input("Login code: ").strip()

    try:
        await client.sign_in(phone, code=code)
    except SessionPasswordNeededError:
        pw = getpass.getpass("2FA password (hidden input): ")
        # Reveal exactly what was captured, incl. hidden/trailing chars.
        print(f"  -> received {len(pw)} characters; repr = {pw!r}")
        try:
            await client.sign_in(password=pw)
        except PasswordHashInvalidError:
            print(
                "\nRESULT: PasswordHashInvalidError — raw Telethon ALSO rejects "
                "this password.\nThe problem is NOT our app: the password string "
                "being entered does not match the account's 2FA password."
            )
            await client.disconnect()
            return

    me = await client.get_me()
    print(
        f"\nRESULT: SUCCESS — logged in as {me.first_name or ''} "
        f"(@{me.username}) id={me.id}.\nIf the app fails with the same password, "
        "the bug is in our app."
    )
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
