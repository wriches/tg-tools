"""The destructive action: remove the target from a single group.

Pure Telegram logic — no storage, no audit. The app layer orchestrates over a
list of groups and records an audit trail. Removal is gated upstream (the UI
shows a dry-run plan and requires confirmation); this function still re-checks
the operator's rights so it can never act on a group it shouldn't.

Modes:
  ban=False -> kick (removed, may rejoin)   [default, least aggressive]
  ban=True  -> ban  (removed and blocked from rejoining; supergroups/channels)
"""
from __future__ import annotations

import logging

from telethon import TelegramClient
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    UserAdminInvalidError,
    UserNotParticipantError,
)
from telethon.tl.types import Channel

from .client import _humanize
from .models import RemovalOutcome

log = logging.getLogger(__name__)


async def remove_from_group(
    client: TelegramClient, group, target, *, ban: bool = False
) -> RemovalOutcome:
    title = getattr(group, "title", str(getattr(group, "id", "?")))
    gid = getattr(group, "id", 0)

    def outcome(ok: bool, action: str, detail: str | None = None) -> RemovalOutcome:
        return RemovalOutcome(group_id=gid, group_title=title, ok=ok, action=action, detail=detail)

    # Attempt directly and let Telegram enforce permissions (a pre-check via
    # get_permissions is unreliable on basic groups). Errors are mapped below.
    is_channel = isinstance(group, Channel)
    try:
        if ban and is_channel:
            await client.edit_permissions(group, target, view_messages=False)
            return outcome(True, "banned")
        await client.kick_participant(group, target)
        if ban and not is_channel:
            return outcome(True, "kicked", "Basic group: removed, but it can't permanently ban — they could be re-added.")
        return outcome(True, "kicked")
    except UserNotParticipantError:
        return outcome(True, "not_member", "Target was not a member (already removed or left).")
    except UserAdminInvalidError:
        return outcome(False, "failed", "Target is an admin here; demote them first.")
    except ChatAdminRequiredError:
        return outcome(False, "failed", "Missing the required admin rights to remove members.")
    except FloodWaitError as exc:
        return outcome(False, "failed", f"Rate-limited by Telegram; wait {_humanize(exc.seconds)} and retry.")
    except Exception as exc:  # noqa: BLE001 - RPCError and any Telethon quirk -> per-group failure, never a 500
        log.warning("removal failed for group %s: %s", gid, exc)
        return outcome(False, "failed", str(exc))
