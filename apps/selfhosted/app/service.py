"""Single-user orchestration: manages the authorized client and login flow,
delegating Telegram work to the shared core and persistence to session_store.
"""
from __future__ import annotations

from telethon import TelegramClient

from tg_tools_core.client import LoginSession, build_client, user_brief
from tg_tools_core.exceptions import NotAuthorizedError
from tg_tools_core.contact import send_message as core_send_message
from tg_tools_core.models import RemovalOutcome, ScanResult, SendOutcome, TargetProfile, UserBrief
from tg_tools_core.remove import remove_from_group
from tg_tools_core.scan import fetch_profile, scan_common_groups

from . import db, session_store
from .config import get_settings


class SelfHostedService:
    def __init__(self) -> None:
        self._client: TelegramClient | None = None
        self._login: LoginSession | None = None

    # ---- authorized client ----
    async def get_client(self) -> TelegramClient:
        if self._client is not None and self._client.is_connected():
            if await self._client.is_user_authorized():
                return self._client

        session = session_store.load()
        if not session:
            raise NotAuthorizedError("Not logged in.")

        s = get_settings()
        client = build_client(session, s.api_id, s.api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            session_store.clear()
            raise NotAuthorizedError("Session expired. Please log in again.")
        self._client = client
        return client

    async def status(self) -> tuple[bool, UserBrief | None]:
        try:
            client = await self.get_client()
        except NotAuthorizedError:
            return False, None
        return True, user_brief(await client.get_me())

    # ---- login flow ----
    async def send_code(self, phone: str) -> None:
        s = get_settings()
        self._login = LoginSession(s.api_id, s.api_hash)
        await self._login.send_code(phone)

    async def sign_in_code(self, code: str) -> UserBrief | None:
        """Returns the user on success, or None if 2FA password is still needed."""
        if self._login is None:
            raise NotAuthorizedError("No login in progress. Request a code first.")
        needs_password = await self._login.sign_in_code(code)
        if needs_password:
            return None
        return await self._finish()

    async def sign_in_password(self, password: str) -> UserBrief:
        if self._login is None:
            raise NotAuthorizedError("No login in progress.")
        await self._login.sign_in_password(password)
        return await self._finish()

    async def _finish(self) -> UserBrief:
        assert self._login is not None
        session_store.save(self._login.session_string())
        self._client = self._login.client
        me = await self._login.me()
        self._login = None
        return me

    # ---- scan & remove ----
    async def get_profile(self, handle: str) -> TargetProfile:
        client = await self.get_client()
        return await fetch_profile(client, handle)

    async def scan(self, handle: str, on_progress=None) -> ScanResult:
        client = await self.get_client()
        result = await scan_common_groups(client, handle, on_progress=on_progress)
        # Persist the session so the entity access-hashes gathered during the scan
        # survive a restart — removal resolves groups by id and needs them.
        session_store.save(client.session.save())
        return result

    async def remove_target(
        self, target_id: int, group_ids: list[int], ban: bool
    ) -> list[RemovalOutcome]:
        client = await self.get_client()
        target = await client.get_entity(target_id)
        target_handle = getattr(target, "username", None)

        outcomes: list[RemovalOutcome] = []
        for gid in group_ids:
            try:
                group = await client.get_entity(gid)
            except Exception as exc:  # entity not cached / not found
                outcome = RemovalOutcome(
                    group_id=gid, group_title=str(gid), ok=False, action="failed",
                    detail=f"Could not resolve group (try re-scanning): {exc}",
                )
            else:
                outcome = await remove_from_group(client, group, target, ban=ban)
            db.audit_add(
                target_id=getattr(target, "id", None), target_handle=target_handle,
                group_id=outcome.group_id, group_title=outcome.group_title,
                action=outcome.action, ok=outcome.ok, detail=outcome.detail,
            )
            outcomes.append(outcome)
        return outcomes

    async def send_message(
        self, admin_id: int, text: str, target_handle: str | None
    ) -> SendOutcome:
        client = await self.get_client()
        label = str(admin_id)
        try:
            user = await client.get_entity(admin_id)
            label = user_brief(user).name or (
                f"@{user.username}" if getattr(user, "username", None) else str(admin_id)
            )
            outcome = await core_send_message(client, user, text)
        except Exception as exc:  # noqa: BLE001 - resolution failure -> failed outcome
            outcome = SendOutcome(admin_id=admin_id, ok=False,
                                  detail=f"Could not resolve recipient: {exc}")
        db.audit_add(
            target_id=None, target_handle=target_handle, group_id=0,
            group_title=f"DM to {label}", action="messaged",
            ok=outcome.ok, detail=outcome.detail,
        )
        return outcome

    async def logout(self) -> None:
        if self._client is not None and self._client.is_connected():
            try:
                await self._client.log_out()
            finally:
                self._client = None
        session_store.clear()


service = SelfHostedService()
