"""Build a group and populate it: resolve pasted handles / contacts to users,
create a supergroup (or use an existing group), and add people — falling back to
an invite link for anyone whose privacy settings block a direct add.

Tenancy-agnostic; persists nothing. The app layer streams progress and records
the audit trail. Two things dominate the design:

* **Per-user outcomes.** Adds are attempted one user at a time (not batched) so
  each person gets a clean result — added / needs-invite / already-member /
  failed — rather than one privacy-restricted user failing a whole batch.
* **Visible rate-limiting.** Resolving handles and adding members are both
  heavily flood-limited by Telegram; like the scan, we lower
  `flood_sleep_threshold` so waits surface as progress instead of a silent hang.
"""
from __future__ import annotations

import asyncio
import logging
import re

from telethon import TelegramClient
from telethon.errors import (
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerFloodError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
    UserChannelsTooMuchError,
    UserKickedError,
    UserNotMutualContactError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserPrivacyRestrictedError,
    UsersTooMuchError,
)
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    InviteToChannelRequest,
    TogglePreHistoryHiddenRequest,
)
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.messages import (
    AddChatUserRequest,
    EditChatDefaultBannedRightsRequest,
    ExportChatInviteRequest,
)
from telethon.tl.types import ChatBannedRights, Channel, Chat, User

from .client import user_brief
from .models import (
    AddableGroup,
    AddOutcome,
    GroupSettings,
    ResolveResult,
    UnresolvedInput,
    UserBrief,
)
from .scan import normalize_handle

log = logging.getLogger(__name__)

_HANDLE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{3,31}$")
_RESOLVE_CONCURRENCY = 3
_FLOOD_THRESHOLD = 5      # waits above this surface as progress (see module doc)
_MAX_FLOOD_WAIT = 150     # give up rather than hang for a very long wait
_MAX_IDENTIFIERS = 500    # cap a pasted list so a huge blob can't stall the run
_ADD_DELAY = 1.0          # gentle throttle between adds (Telegram limits these hard)


def parse_identifiers(raw: str) -> list[str]:
    """Split pasted text into unique bare usernames, accepting @name, name,
    t.me/name and full URLs in any space/comma/newline-separated arrangement."""
    seen: set[str] = set()
    out: list[str] = []
    for token in re.split(r"[\s,;]+", raw or ""):
        h = normalize_handle(token)
        if not h:
            continue
        key = h.lower()
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


async def get_contacts(client: TelegramClient) -> list[UserBrief]:
    """The operator's Telegram contacts, name-sorted, deleted accounts dropped."""
    res = await client(GetContactsRequest(hash=0))
    briefs = [
        user_brief(u)
        for u in getattr(res, "users", [])
        if not getattr(u, "deleted", False)
    ]
    briefs.sort(key=lambda b: (b.name or b.username or "").lower())
    return briefs


def _can_invite(entity) -> bool:
    """Best-effort: can I add members here? Creators always can; admins per their
    invite right; otherwise members can unless the group bans it by default. The
    add attempt still enforces the real permission."""
    if getattr(entity, "creator", False):
        return True
    ar = getattr(entity, "admin_rights", None)
    if ar is not None:
        return bool(getattr(ar, "invite_users", False))
    dbr = getattr(entity, "default_banned_rights", None)
    if dbr is not None:
        return not bool(getattr(dbr, "invite_users", False))
    return True


async def list_addable_groups(client: TelegramClient, on_progress=None) -> list[AddableGroup]:
    """Groups and supergroups (not broadcast channels) you can add members to.
    Paging all dialogs is slow for busy accounts, so `on_progress(scanned, found)`
    fires periodically to drive a loading indicator."""
    groups: list[AddableGroup] = []
    scanned = 0
    async for dialog in client.iter_dialogs():
        scanned += 1
        ent = dialog.entity
        if isinstance(ent, Chat):
            gtype = "group"
        elif isinstance(ent, Channel) and getattr(ent, "megagroup", False):
            gtype = "supergroup"
        else:
            if on_progress and scanned % 50 == 0:
                await on_progress(scanned, len(groups))
            continue
        if getattr(ent, "deactivated", False) or getattr(ent, "left", False):
            continue
        if not _can_invite(ent):
            continue
        groups.append(
            AddableGroup(
                id=ent.id,
                title=getattr(ent, "title", "(untitled)"),
                username=getattr(ent, "username", None),
                type=gtype,
                members_count=getattr(ent, "participants_count", None),
            )
        )
        if on_progress and scanned % 50 == 0:
            await on_progress(scanned, len(groups))
    if on_progress:
        await on_progress(scanned, len(groups))
    groups.sort(key=lambda g: g.title.lower())
    return groups


def _record(handle: str, kind: str, val, resolved: dict, unresolved: list, exclude_ids: set) -> None:
    if kind == "bad":
        unresolved.append(UnresolvedInput(input=handle, reason=val))
        return
    ent = val
    if not isinstance(ent, User):
        unresolved.append(UnresolvedInput(input=handle, reason="That's a group or channel, not a user."))
    elif getattr(ent, "deleted", False):
        unresolved.append(UnresolvedInput(input=handle, reason="Deleted account."))
    elif ent.id in exclude_ids:
        unresolved.append(UnresolvedInput(input=handle, reason="That's you."))
    else:
        resolved[ent.id] = user_brief(ent)  # keyed by id -> dedupes across spellings


async def resolve_users(
    client: TelegramClient, identifiers: list[str], exclude_ids=None, on_progress=None
) -> ResolveResult:
    """Resolve bare usernames to users, with visible flood-wait backoff. Returns
    resolved users (deduped by id) and an unresolved list with reasons."""
    exclude_ids = set(exclude_ids or ())
    idents = list(dict.fromkeys(identifiers))[:_MAX_IDENTIFIERS]
    total = len(idents)
    if on_progress:
        await on_progress(0, total)

    resolved: dict[int, UserBrief] = {}
    unresolved: list[UnresolvedInput] = []
    sem = asyncio.Semaphore(_RESOLVE_CONCURRENCY)
    lock = asyncio.Lock()
    done = 0
    prev = getattr(client, "flood_sleep_threshold", 60)
    client.flood_sleep_threshold = _FLOOD_THRESHOLD

    async def one(handle: str) -> None:
        nonlocal done
        kind, val = "bad", "Not a valid username."
        if _HANDLE_RE.match(handle):
            while True:
                try:
                    async with sem:
                        val = await client.get_entity(handle)
                    kind = "ent"
                    break
                except FloodWaitError as exc:
                    wait = int(getattr(exc, "seconds", 5))
                    if wait > _MAX_FLOOD_WAIT:
                        kind, val = "bad", f"Rate-limited (needs {wait}s); skipped."
                        break
                    if on_progress:
                        await on_progress(done, total, wait)
                    await asyncio.sleep(wait + 1)
                except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
                    kind, val = "bad", "No Telegram user with that username."
                    break
                except Exception as exc:  # noqa: BLE001 - any quirk -> unresolved, never a 500
                    kind, val = "bad", f"Couldn't resolve: {exc}"
                    break
        async with lock:
            done += 1
            _record(handle, kind, val, resolved, unresolved, exclude_ids)
            if on_progress:
                await on_progress(done, total)

    try:
        await asyncio.gather(*(one(h) for h in idents))
    finally:
        client.flood_sleep_threshold = prev

    resolved_list = sorted(resolved.values(), key=lambda b: (b.name or b.username or "").lower())
    unresolved.sort(key=lambda u: u.input.lower())
    return ResolveResult(resolved=resolved_list, unresolved=unresolved)


def _banned_rights(s: GroupSettings) -> ChatBannedRights:
    """Translate the (True = allowed) settings into Telegram's ChatBannedRights
    (True = banned). Media/sticker toggles cover the granular sub-flags too, so
    the setting behaves the same regardless of client layer."""
    no = lambda allowed: not allowed
    return ChatBannedRights(
        until_date=0,
        send_messages=no(s.send_messages),
        send_plain=no(s.send_messages),
        send_media=no(s.send_media),
        send_photos=no(s.send_media),
        send_videos=no(s.send_media),
        send_roundvideos=no(s.send_media),
        send_audios=no(s.send_media),
        send_voices=no(s.send_media),
        send_docs=no(s.send_media),
        send_stickers=no(s.send_stickers),
        send_gifs=no(s.send_stickers),
        send_games=no(s.send_stickers),
        send_inline=no(s.send_stickers),
        send_polls=no(s.send_polls),
        embed_links=no(s.embed_links),
        invite_users=no(s.invite_users),
        pin_messages=no(s.pin_messages),
        change_info=no(s.change_info),
    )


async def apply_group_settings(client: TelegramClient, channel, s: GroupSettings) -> list[str]:
    """Best-effort: apply history visibility + default member permissions to a
    freshly created group. Returns a list of human-readable warnings for any
    setting that couldn't be applied (the group itself is already created)."""
    warnings: list[str] = []
    try:
        # enabled=True hides pre-join history from new members.
        await client(TogglePreHistoryHiddenRequest(channel, enabled=not s.history_visible))
    except Exception as exc:  # noqa: BLE001
        log.warning("history visibility failed: %s", exc)
        warnings.append("Couldn't set chat-history visibility.")
    try:
        await client(EditChatDefaultBannedRightsRequest(peer=channel, banned_rights=_banned_rights(s)))
    except Exception as exc:  # noqa: BLE001
        log.warning("default permissions failed: %s", exc)
        warnings.append("Couldn't set member permissions.")
    return warnings


async def create_supergroup(client: TelegramClient, title: str, about: str = "") -> Channel:
    """Create an empty megagroup you own; members are invited separately."""
    res = await client(
        CreateChannelRequest(title=title[:255], about=(about or "")[:255], megagroup=True)
    )
    return res.chats[0]


def _outcome(user, status: str, detail: str | None = None,
             abort: bool = False, stop_all: bool = False) -> AddOutcome:
    b = user_brief(user)
    return AddOutcome(
        user_id=b.id, name=b.name, username=b.username,
        status=status, detail=detail, abort=abort, stop_all=stop_all,
    )


def _missing_invitee_ids(res) -> set[int]:
    """Ids Telegram reported it *couldn't* add. On current layers `inviteToChannel`
    / `addChatUser` return `messages.InvitedUsers` and list privacy/eligibility
    blocks here as `missing_invitees` instead of raising — so a silent success is
    not proof the user was actually added."""
    out: set[int] = set()
    for m in getattr(res, "missing_invitees", None) or []:
        uid = getattr(m, "user_id", None)
        if uid is not None:
            out.add(uid)
    return out


async def add_user(client: TelegramClient, group, user: User) -> AddOutcome:
    """Attempt to add one user; map Telegram's response/errors to a per-user
    outcome. Privacy/eligibility blocks become `needs_invite` (send them the
    link) — detected both from `missing_invitees` (current layers) and from a
    raised `UserPrivacyRestrictedError` (older layers / basic groups); group-level
    failures set `abort` so the caller can stop the whole run."""
    try:
        if isinstance(group, Channel):
            res = await client(InviteToChannelRequest(group, [user]))
        else:
            res = await client(AddChatUserRequest(group.id, user, fwd_limit=0))
        if getattr(user, "id", None) in _missing_invitee_ids(res):
            return _outcome(user, "needs_invite", "Couldn't be added (privacy/eligibility) — send them the invite link.")
        return _outcome(user, "added")
    except UserPrivacyRestrictedError:
        return _outcome(user, "needs_invite", "Privacy settings block adding — send them the invite link.")
    except UserNotMutualContactError:
        return _outcome(user, "needs_invite", "Not a mutual contact — send them the invite link.")
    except UserAlreadyParticipantError:
        return _outcome(user, "already_member", "Already in the group.")
    except UserChannelsTooMuchError:
        return _outcome(user, "failed", "They're in too many groups/channels.")
    except (UserKickedError, UserBannedInChannelError):
        return _outcome(user, "failed", "They were banned from this group.")
    except InputUserDeactivatedError:
        return _outcome(user, "failed", "Deleted account.")
    except UsersTooMuchError:
        return _outcome(user, "failed", "The group is full.", abort=True)
    except (ChatAdminRequiredError, ChatWriteForbiddenError):
        return _outcome(user, "failed", "You don't have permission to add members here.", abort=True)
    except PeerFloodError:
        return _outcome(user, "failed", "Telegram is spam-limiting your account — stop adding for a while.", abort=True, stop_all=True)
    except FloodWaitError:
        raise  # visible backoff handled by add_users
    except Exception as exc:  # noqa: BLE001 - any Telethon quirk -> per-user failure
        log.warning("add failed for %s: %s", getattr(user, "id", "?"), exc)
        return _outcome(user, "failed", str(exc))


async def add_users(
    client: TelegramClient, group, user_ids: list[int], on_progress=None
) -> tuple[list[AddOutcome], str | None, bool]:
    """Add each user (by id, resolved from the client's cache) sequentially, with
    a gentle throttle and visible flood-wait backoff. Returns the outcomes, the
    abort reason if the run was cut short by a group-level problem, and a
    `stop_all` flag set when the abort is account-level (spam limit) and the
    caller should stop adding to *any* further groups too.

    on_progress(done, total, outcome=None, wait=None) fires after each user and
    during a flood wait."""
    total = len(user_ids)
    outcomes: list[AddOutcome] = []
    aborted: str | None = None
    stop_all = False
    done = 0
    prev = getattr(client, "flood_sleep_threshold", 60)
    client.flood_sleep_threshold = _FLOOD_THRESHOLD
    try:
        for uid in user_ids:
            try:
                user = await client.get_entity(uid)
            except Exception as exc:  # entity fell out of cache
                outcome = AddOutcome(
                    user_id=uid, status="failed",
                    detail=f"Couldn't resolve user (re-resolve and retry): {exc}",
                )
            else:
                while True:
                    try:
                        outcome = await add_user(client, group, user)
                        break
                    except FloodWaitError as exc:
                        wait = int(getattr(exc, "seconds", 5))
                        if wait > _MAX_FLOOD_WAIT:
                            outcome = _outcome(user, "failed", f"Rate-limited ({wait}s); skipped.")
                            break
                        if on_progress:
                            await on_progress(done, total, None, wait)
                        await asyncio.sleep(wait + 1)
            outcomes.append(outcome)
            done += 1
            if on_progress:
                await on_progress(done, total, outcome, None)
            if outcome.abort:
                aborted = outcome.detail
                stop_all = outcome.stop_all
                break
            if done < total:
                await asyncio.sleep(_ADD_DELAY)
    finally:
        client.flood_sleep_threshold = prev
    return outcomes, aborted, stop_all


async def export_invite(client: TelegramClient, group) -> str | None:
    """A shareable invite link for the group, or None if export fails."""
    try:
        res = await client(ExportChatInviteRequest(group))
        return getattr(res, "link", None)
    except Exception as exc:  # noqa: BLE001 - link is best-effort
        log.warning("invite export failed: %s", exc)
        return None
