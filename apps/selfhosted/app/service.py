"""Single-user orchestration: manages the authorized client and login flow,
delegating Telegram work to the shared core and persistence to session_store.
"""
from __future__ import annotations

from telethon import TelegramClient

from tg_tools_core.client import LoginSession, build_client, user_brief
from tg_tools_core.exceptions import NotAuthorizedError
from tg_tools_core.contact import send_message as core_send_message
from tg_tools_core.build import (
    add_users,
    create_supergroup,
    export_invite,
    get_contacts as core_get_contacts,
    list_addable_groups,
    parse_identifiers,
    resolve_users,
)
from tg_tools_core.models import (
    AddableGroup,
    RemovalOutcome,
    ResolveResult,
    ScanResult,
    SendOutcome,
    TargetProfile,
    UserBrief,
)
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

    # ---- builder ----
    async def get_contacts(self) -> list[UserBrief]:
        client = await self.get_client()
        return await core_get_contacts(client)

    async def list_groups(self, on_progress=None) -> list[AddableGroup]:
        client = await self.get_client()
        groups = await list_addable_groups(client, on_progress=on_progress)
        # Persist so the groups' access-hashes survive a restart — a later add
        # resolves each group by id (possibly from a cached list) and needs them.
        session_store.save(client.session.save())
        return groups

    async def resolve_text(self, text: str, on_progress=None) -> ResolveResult:
        client = await self.get_client()
        me = await client.get_me()
        result = await resolve_users(
            client, parse_identifiers(text),
            exclude_ids={getattr(me, "id", 0)}, on_progress=on_progress,
        )
        # Persist the session so resolved access-hashes survive to the add step.
        session_store.save(client.session.save())
        return result

    async def build_and_add(
        self, *, mode: str, title: str | None, group_ids: list[int],
        user_ids: list[int], on_event=None,
    ) -> dict:
        """Add the resolved users to one destination (create mode) or several
        existing groups. Each group is processed in turn; a spam limit stops the
        whole run, a group-level failure only skips that group."""
        client = await self.get_client()

        # Resolve destinations up front: (info, entity_or_None, error_or_None).
        dests: list[tuple[dict, object, str | None]] = []
        if mode == "create":
            if not (title or "").strip():
                raise ValueError("Enter a name for the new group.")
            group = await create_supergroup(client, title.strip())
            dests.append((
                {"id": group.id, "title": getattr(group, "title", ""),
                 "username": getattr(group, "username", None), "created": True},
                group, None,
            ))
        else:
            if not group_ids:
                raise ValueError("Select at least one group to add people to.")
            for gid in group_ids:
                try:
                    group = await client.get_entity(gid)
                    dests.append((
                        {"id": gid, "title": getattr(group, "title", str(gid)),
                         "username": getattr(group, "username", None), "created": False},
                        group, None,
                    ))
                except Exception as exc:  # noqa: BLE001 - one group can't resolve
                    dests.append((
                        {"id": gid, "title": str(gid), "username": None, "created": False},
                        None, f"Couldn't resolve group (re-load the group list): {exc}",
                    ))

        if on_event:
            await on_event({
                "type": "plan",
                "groups": [d[0] for d in dests],
                "total_groups": len(dests), "total_users": len(user_ids),
            })

        results: list[dict] = []
        stopped_all: str | None = None
        for gi, (info, group, err) in enumerate(dests):
            if on_event:
                await on_event({"type": "group_start", "group_index": gi, "group": info})
            if err is not None:
                gres = {"group": info, "outcomes": [], "invite_link": None, "aborted": err}
                results.append(gres)
                if on_event:
                    await on_event({"type": "group_done", "group_index": gi, **gres})
                continue

            async def prog(done, total, outcome=None, wait=None, gi=gi):
                if on_event:
                    await on_event({
                        "type": "progress", "group_index": gi,
                        "done": done, "total": total, "wait": wait,
                        "outcome": outcome.model_dump() if outcome else None,
                    })

            outcomes, aborted, stop_all = await add_users(
                client, group, user_ids, on_progress=prog
            )
            invite_link = None
            if any(o.status == "needs_invite" for o in outcomes):
                invite_link = await export_invite(client, group)
            for o in outcomes:
                db.audit_add(
                    target_id=o.user_id, target_handle=o.username,
                    group_id=info["id"], group_title=info["title"],
                    action=f"add:{o.status}",
                    ok=o.status in ("added", "already_member"), detail=o.detail,
                )
            gres = {
                "group": info, "outcomes": [o.model_dump() for o in outcomes],
                "invite_link": invite_link, "aborted": aborted,
            }
            results.append(gres)
            if on_event:
                await on_event({"type": "group_done", "group_index": gi, **gres})
            if stop_all:
                stopped_all = aborted
                break

        # Persist so new/added entity access-hashes survive a restart.
        session_store.save(client.session.save())
        return {"results": results, "stopped_all": stopped_all}

    async def logout(self) -> None:
        if self._client is not None and self._client.is_connected():
            try:
                await self._client.log_out()
            finally:
                self._client = None
        session_store.clear()


service = SelfHostedService()
