"""API request/response models. Domain shapes are reused from the core."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from tg_tools_core.models import GroupSettings, UserBrief


class SendCodeRequest(BaseModel):
    phone: str


class SignInRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str


class StatusResponse(BaseModel):
    authorized: bool
    me: UserBrief | None = None


class AuthStepResponse(BaseModel):
    next: Literal["code", "password", "done"]
    me: UserBrief | None = None


class ScanRequest(BaseModel):
    handle: str


class RemoveRequest(BaseModel):
    target_id: int
    group_ids: list[int]
    ban: bool = False


class ContactSendRequest(BaseModel):
    admin_id: int
    text: str
    target_handle: str | None = None


class ResolveRequest(BaseModel):
    text: str


class BuildTarget(BaseModel):
    group_id: int
    type: str | None = None
    user_ids: list[int] = []


class BuildRequest(BaseModel):
    mode: Literal["create", "existing"]
    title: str | None = None
    user_ids: list[int] = []               # create mode: members for the new group
    targets: list[BuildTarget] = []        # existing mode: per-group member lists
    settings: GroupSettings | None = None
