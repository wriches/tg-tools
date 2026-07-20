"""Send a direct message to one admin/owner. Pure Telegram logic — the app
layer resolves the recipient, throttles, and records an audit trail.

Messaging non-contacts is the highest account-ban-risk action in this tool, so
errors (privacy blocks, spam limits, flood waits) are mapped to clear, per-
message outcomes rather than raised.
"""
from __future__ import annotations

import logging

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
)

from .client import _humanize
from .models import SendOutcome

log = logging.getLogger(__name__)


async def send_message(client: TelegramClient, user, text: str) -> SendOutcome:
    uid = getattr(user, "id", 0)
    try:
        await client.send_message(user, text)
        return SendOutcome(admin_id=uid, ok=True)
    except UserPrivacyRestrictedError:
        return SendOutcome(admin_id=uid, ok=False,
                           detail="They don't accept messages from non-contacts.")
    except PeerFloodError:
        return SendOutcome(admin_id=uid, ok=False, abort=True,
                           detail="Telegram is spam-limiting your account. Stop sending "
                                  "and try again later (sending more risks a ban).")
    except FloodWaitError as exc:
        return SendOutcome(admin_id=uid, ok=False, retry_after=int(exc.seconds),
                           detail=f"Rate-limited; wait {_humanize(exc.seconds)} and retry.")
    except Exception as exc:  # noqa: BLE001 - any quirk -> per-message failure
        log.warning("send failed to %s: %s", uid, exc)
        return SendOutcome(admin_id=uid, ok=False, detail=str(exc))
