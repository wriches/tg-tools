"""Read-only scan: find groups in common with a target user and classify, for
each, whether the operator can remove the target directly or must ask an admin.

Nothing in this module mutates Telegram state.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re

from telethon import TelegramClient
from telethon.errors import ChatAdminRequiredError, FloodWaitError, RPCError
from telethon.tl.functions.messages import GetCommonChatsRequest, GetFullChatRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    Channel,
    Chat,
    ChannelParticipantCreator,
    ChannelParticipantsAdmins,
    ChatParticipantAdmin,
    ChatParticipantCreator,
    ChatParticipantsForbidden,
)

from .client import user_brief
from .models import GroupResult, MyRights, ScanResult, TargetProfile, UserBrief

log = logging.getLogger(__name__)

_COMMON_CHATS_PAGE = 100
_HANDLE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{3,31}$")


def normalize_handle(handle: str) -> str:
    """Reduce a pasted @mention or t.me link to a bare username."""
    h = (handle or "").strip()
    h = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", h, flags=re.IGNORECASE)
    return h.lstrip("@").strip()


def _chat_type(chat) -> str:
    if isinstance(chat, Channel):
        return "supergroup" if getattr(chat, "megagroup", False) else "channel"
    return "group"


def _chat_link(chat) -> str | None:
    """A tg:// deep link that opens the chat in the Telegram app, or None for
    basic (legacy) groups which have no addressable link.

    Public chats resolve by @username; private supergroups/channels open by
    internal id (post=1 lands at the top of the chat). We use tg:// rather than
    https://t.me/... because the c/<id> web form has no public page and just
    redirects to telegram.org in a browser.
    """
    username = getattr(chat, "username", None)
    if username:
        return f"tg://resolve?domain={username}"
    if isinstance(chat, Channel):
        return f"tg://privatepost?channel={chat.id}&post=1"
    return None


async def _common_chats(client: TelegramClient, target_input) -> list:
    """Page through all chats in common with the target."""
    chats: list = []
    max_id = 0
    while True:
        res = await client(
            GetCommonChatsRequest(user_id=target_input, max_id=max_id, limit=_COMMON_CHATS_PAGE)
        )
        page = res.chats
        chats.extend(page)
        if len(page) < _COMMON_CHATS_PAGE:
            break
        max_id = page[-1].id
    return chats


def _rights_from_chat_participant(p) -> MyRights:
    if isinstance(p, ChatParticipantCreator):
        return MyRights(is_creator=True, can_ban=True)
    if isinstance(p, ChatParticipantAdmin):
        return MyRights(can_ban=True)
    return MyRights()


def _skip_admin(u, exclude_ids: set[int]) -> bool:
    """Exclude admins we can't or shouldn't ask: bots, deleted accounts, the
    target themselves, and the operator."""
    return (
        bool(getattr(u, "bot", False))
        or bool(getattr(u, "deleted", False))
        or getattr(u, "id", None) in exclude_ids
    )


async def _resolve_target(client: TelegramClient, handle: str):
    handle = normalize_handle(handle)
    if not handle:
        raise ValueError("Enter a username to scan, e.g. username.")
    if not _HANDLE_RE.match(handle):
        raise ValueError(
            "That doesn't look like a valid username — use 4–32 letters, digits "
            "or underscores, starting with a letter."
        )
    return await client.get_entity(handle)


async def _build_profile(client: TelegramClient, target) -> TargetProfile:
    base = user_brief(target).model_dump()
    bio = None
    try:
        full = await client(GetFullUserRequest(target))
        bio = getattr(full.full_user, "about", None)
    except Exception as exc:  # noqa: BLE001 - profile extras are best-effort
        log.debug("bio lookup failed: %s", exc)
    photo = None
    try:
        data = await client.download_profile_photo(target, file=bytes, download_big=False)
        if data:
            photo = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        log.debug("photo download failed: %s", exc)
    return TargetProfile(**base, bio=bio, photo=photo)


async def fetch_profile(client: TelegramClient, handle: str) -> TargetProfile:
    """Resolve a handle and return its profile (name, username, bio, photo)."""
    return await _build_profile(client, await _resolve_target(client, handle))


async def _classify_basic(client, chat, me_id, target_id, exclude_ids):
    """Classify a basic (legacy) group from ONE GetFullChat call, returning
    (my_rights, target_is_owner, admins). The generic path would issue three
    separate participant queries per group (each a full GetFullChat for legacy
    groups), and firing many at once trips Telegram flood-waits."""
    try:
        full = await client(GetFullChatRequest(chat.id))
    except FloodWaitError:
        raise  # handled (with visible backoff) by the scan loop
    except Exception as exc:  # noqa: BLE001
        log.debug("GetFullChat failed for %s: %s", getattr(chat, "id", "?"), exc)
        return MyRights(), False, []
    parts = full.full_chat.participants
    if isinstance(parts, ChatParticipantsForbidden):
        # Only our own participant is visible; can't see target role or admins.
        return _rights_from_chat_participant(parts.self_participant), False, []
    users_by_id = {u.id: u for u in full.users}
    rights = MyRights()
    target_owner = False
    admins: list[UserBrief] = []
    for p in parts.participants:
        uid = getattr(p, "user_id", None)
        if uid == me_id:
            rights = _rights_from_chat_participant(p)
        if uid == target_id and isinstance(p, ChatParticipantCreator):
            target_owner = True
        if isinstance(p, (ChatParticipantCreator, ChatParticipantAdmin)):
            u = users_by_id.get(uid)
            if u is not None and not _skip_admin(u, exclude_ids):
                admins.append(user_brief(u, isinstance(p, ChatParticipantCreator)))
    admins.sort(key=lambda a: (not a.is_owner, a.name.lower()))
    return rights, target_owner, admins


async def _classify_channel(client, chat, me_id, target_id, exclude_ids):
    """Classify a channel/supergroup from ONE getParticipants(admins) call,
    returning (my_rights, target_is_owner, admins). The admin list includes the
    creator and everyone's rights, so my own rights and the target's ownership
    fall out of the same query."""
    try:
        members = await client.get_participants(chat, filter=ChannelParticipantsAdmins())
    except FloodWaitError:
        raise
    except (ChatAdminRequiredError, RPCError) as exc:
        # Fall back to just my own rights so bucketing still works.
        log.debug("admin list failed for %s: %s", getattr(chat, "id", "?"), exc)
        try:
            perms = await client.get_permissions(chat, "me")
        except FloodWaitError:
            raise
        except Exception:  # noqa: BLE001
            return MyRights(), False, []
        return (
            MyRights(is_creator=bool(getattr(perms, "is_creator", False)),
                     can_ban=bool(getattr(perms, "ban_users", False))),
            False, [],
        )

    rights = MyRights()
    target_owner = False
    admins: list[UserBrief] = []
    for u in members:
        p = getattr(u, "participant", None)
        is_creator = isinstance(p, ChannelParticipantCreator)
        uid = getattr(u, "id", None)
        if uid == me_id:
            can_ban = is_creator or bool(getattr(getattr(p, "admin_rights", None), "ban_users", False))
            rights = MyRights(is_creator=is_creator, can_ban=can_ban)
        if uid == target_id and is_creator:
            target_owner = True
        if not _skip_admin(u, exclude_ids):
            admins.append(user_brief(u, is_creator))
    admins.sort(key=lambda a: (not a.is_owner, a.name.lower()))
    return rights, target_owner, admins


async def _classify_group(
    client: TelegramClient, chat, exclude_ids: set[int], me_id: int, target_id: int
) -> GroupResult:
    if isinstance(chat, Chat):
        rights, target_owner, all_admins = await _classify_basic(
            client, chat, me_id, target_id, exclude_ids
        )
    else:
        rights, target_owner, all_admins = await _classify_channel(
            client, chat, me_id, target_id, exclude_ids
        )

    note: str | None = None
    admins: list[UserBrief] = []
    if target_owner:
        bucket = "no_action"
        note = "Target is the owner of this group and cannot be removed."
    elif rights.is_creator or rights.can_ban:
        bucket = "removable_by_me"
    else:
        bucket = "needs_admin"
        admins = all_admins
        if not admins:
            note = "No contactable admin found (bots excluded); removal may not be possible."

    return GroupResult(
        id=chat.id,
        title=getattr(chat, "title", "(untitled)"),
        username=getattr(chat, "username", None),
        link=_chat_link(chat),
        type=_chat_type(chat),
        members_count=getattr(chat, "participants_count", None),
        my_rights=rights,
        bucket=bucket,
        admins=admins,
        note=note,
    )


_SCAN_CONCURRENCY = 3
# Waits above this raise (so we can show them) instead of Telethon silently
# sleeping; smaller waits are auto-handled transparently.
_SCAN_FLOOD_THRESHOLD = 5
# If Telegram demands a wait longer than this, give up rather than hang for ages.
_MAX_FLOOD_WAIT = 150


async def scan_common_groups(
    client: TelegramClient, handle: str, on_progress=None
) -> ScanResult:
    target = await _resolve_target(client, handle)
    target_input = await client.get_input_entity(target)
    me = await client.get_me()
    me_id = getattr(me, "id", 0)
    target_id = getattr(target, "id", 0)
    # Admins we should never suggest contacting: ourselves and the target.
    exclude_ids = {me_id, target_id}

    chats = await _common_chats(client, target_input)
    total = len(chats)
    if on_progress:
        await on_progress(0, total)

    # Classify groups concurrently (bounded). Surface flood-waits as visible
    # progress instead of letting Telethon silently sleep through them.
    sem = asyncio.Semaphore(_SCAN_CONCURRENCY)
    lock = asyncio.Lock()
    done = 0
    prev_threshold = getattr(client, "flood_sleep_threshold", 60)
    client.flood_sleep_threshold = _SCAN_FLOOD_THRESHOLD

    async def process(chat) -> GroupResult:
        nonlocal done
        while True:
            try:
                async with sem:
                    result = await _classify_group(
                        client, chat, exclude_ids, me_id, target_id
                    )
                break
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 5))
                if wait > _MAX_FLOOD_WAIT:
                    raise
                if on_progress:
                    await on_progress(done, total, wait)
                await asyncio.sleep(wait + 1)
        async with lock:
            done += 1
            if on_progress:
                await on_progress(done, total)
        return result

    try:
        # gather preserves input order, so display order stays stable.
        groups = list(await asyncio.gather(*(process(c) for c in chats)))
    finally:
        client.flood_sleep_threshold = prev_threshold

    summary = {
        "removable_by_me": sum(g.bucket == "removable_by_me" for g in groups),
        "needs_admin": sum(g.bucket == "needs_admin" for g in groups),
        "no_action": sum(g.bucket == "no_action" for g in groups),
        "total": len(groups),
    }
    return ScanResult(target=user_brief(target), groups=groups, summary=summary)
