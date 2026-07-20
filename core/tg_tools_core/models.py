"""Domain models shared across apps (the Telegram-facing data shapes)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Bucket = Literal["removable_by_me", "needs_admin", "no_action"]


class UserBrief(BaseModel):
    id: int
    username: str | None = None
    name: str = ""
    is_owner: bool = False


class TargetProfile(UserBrief):
    bio: str | None = None
    photo: str | None = None  # data: URI of the profile photo, if any


class MyRights(BaseModel):
    is_creator: bool = False
    can_ban: bool = False


class GroupResult(BaseModel):
    id: int
    title: str
    username: str | None = None
    link: str | None = None
    type: Literal["group", "supergroup", "channel"]
    members_count: int | None = None
    my_rights: MyRights
    bucket: Bucket
    admins: list[UserBrief] = []
    note: str | None = None


class ScanResult(BaseModel):
    target: UserBrief
    groups: list[GroupResult]
    summary: dict[str, int]


# Outcome of attempting to remove the target from one group.
# action: kicked | banned | not_member | skipped | failed
class RemovalOutcome(BaseModel):
    group_id: int
    group_title: str
    ok: bool
    action: str
    detail: str | None = None


class SendOutcome(BaseModel):
    admin_id: int
    ok: bool
    detail: str | None = None
    retry_after: int | None = None  # FloodWait: seconds to wait before the next send
    abort: bool = False             # hard spam limit (PeerFlood): stop sending entirely


# ---- Builder tool ----

class AddableGroup(BaseModel):
    """An existing group you can add members to (create mode aside)."""
    id: int
    title: str
    username: str | None = None
    type: Literal["group", "supergroup"]
    members_count: int | None = None


class UnresolvedInput(BaseModel):
    input: str
    reason: str


class ResolveResult(BaseModel):
    resolved: list[UserBrief]
    unresolved: list[UnresolvedInput]


# Outcome of attempting to add one user to a group.
# status: added | needs_invite | already_member | failed
class AddOutcome(BaseModel):
    user_id: int
    name: str = ""
    username: str | None = None
    status: str
    detail: str | None = None
    # True for a failure that should stop adding to the current group
    # (missing rights, group full, spam limit).
    abort: bool = False
    # True when the failure is account-level (spam limit) and should stop the
    # entire run across all groups, not just the current one.
    stop_all: bool = False


class AuditEntry(BaseModel):
    ts: str
    target_handle: str | None = None
    group_title: str
    action: str
    ok: bool
    detail: str | None = None
