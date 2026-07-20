# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`tg-tools` is a self-hosted suite of Telegram utilities you run against **your own
account** via the MTProto **user API** (Telethon) — *not* the Bot API, which
cannot enumerate shared groups, read group membership, or DM arbitrary users.
There are two tools:
- **Remover**: find groups you share with a target user, remove them where you
  have rights, and draft/send aggregated removal requests to the admins of
  groups where you don't.
- **Builder**: create a supergroup (or reuse an existing group) and bulk-add
  people, resolved from pasted handles and/or selected contacts; anyone blocked
  by privacy settings is offered the group's invite link to DM.

Every scan/remove/send is a **real** Telegram action against real people. The UI
gates destructive actions behind dry-run confirmation and a one-time ToS
acknowledgement; the backend audit-logs every action.

## Setup, run, validate

```bash
# Setup (requirements.txt installs the core package editable via `-e ./core`)
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Run (must be from apps/selfhosted so `app` is importable; reads repo-root .env)
cd apps/selfhosted && uvicorn app.main:app --host 127.0.0.1 --port 8000   # add --reload for dev
# then open http://127.0.0.1:8000
```

Requires a repo-root `.env` (copy `.env.example`) with `TG_API_ID`, `TG_API_HASH`
(from https://my.telegram.org) and a long random `TG_SECRET_KEY`.

**There is no test suite.** Validate changes this way:
- Python: `PYTHONPATH=apps/selfhosted python -c "import tg_tools_core; from app.main import app"` (catches import/wiring errors); `python -m py_compile <file>` for syntax.
- Frontend JS: extract the inline `<script>` from `apps/selfhosted/frontend/index.html` and run `node --check` on it — the frontend is a single static file with no build step.
- Real behavior can only be confirmed by running the app and logging in (needs a live phone/code/2FA), so exercise flows in the browser.

Gotchas: the virtualenv is **not relocatable** (rebuild it with `python3 -m venv`
if the repo folder moves). `login_probe.py` in `scripts/` is a standalone
raw-Telethon login to isolate auth issues.

## Architecture

**Monorepo, deliberately split so multiple deployment targets can share Telegram logic:**

- `core/tg_tools_core/` — a **tenancy-agnostic** package that owns *all* Telegram
  logic and **persists nothing**. Installed editable; apps `import tg_tools_core`.
  Key modules: `client.py` (client factory + `LoginSession` login primitive),
  `scan.py`, `remove.py`, `contact.py` (Remover), `build.py` (Builder),
  `models.py` (Pydantic domain models), `crypto.py`, `exceptions.py`.
- `apps/selfhosted/` — the single-user FastAPI app + a **single static
  `frontend/index.html`** (vanilla JS, no framework/build). It owns everything
  the core doesn't: config, SQLite storage, session persistence, web layer.
- `apps/public/` — a planned removal-only multi-tenant app (README stub only). It
  exists to justify the core/app split: it will reuse `core` with ephemeral
  (non-persisted) sessions and abuse guardrails.

The **core never imports from apps**; apps depend on core. When adding Telegram
behavior, put the API logic in `core` and the storage/web/tenancy concerns in the
app. Login is a good example: `core`'s `LoginSession` drives Telethon through
phone→code→2FA and hands back a session string; the app decides to encrypt and
store it (self-hosted) vs. keep it ephemeral (public).

**App layering** (`apps/selfhosted/app/`): `main.py` (FastAPI routes + the
`/ws/scan` WebSocket) → `service.py` (`SelfHostedService` singleton: orchestrates
the authorized client, login flow, and audit logging) → core. `config.py` loads
`TG_`-prefixed settings from the **repo-root** `.env` (path computed from the
file's location). `session_store.py` + `db.py` are the only persistence (a tiny
SQLite kv table for the encrypted session, plus an `audit` table).

### Session & auth model
Auth is a Telethon user session stored as an encrypted `StringSession` (Fernet
key derived from `TG_SECRET_KEY`) in SQLite. The session is persisted **on login
and again after every scan** — the scan warms Telethon's entity cache with group
access-hashes, and removal resolves groups **by numeric id**, so those hashes
must survive a restart. On any authorized request, `get_client()` reconstructs
and connects the client, and clears the stored session if Telegram reports it
expired.

### Scan pipeline (the performance-critical path)
`scan_common_groups` resolves the target → `messages.getCommonChats` → classifies
each shared group into one of three buckets: `removable_by_me` (you're creator or
have `ban_users`), `needs_admin` (resolve owner/admins to ask), or `no_action`
(you can't act and no admin, or the target owns the group).

Classification is **one participant query per group**, run with bounded
concurrency (`_SCAN_CONCURRENCY`): basic (legacy) groups use a single
`GetFullChat` (`_classify_basic`), supergroups/channels a single
`getParticipants(admins)` (`_classify_channel`) — both derive *my rights, the
target's ownership, and the admin list* from that one call. This matters:
Telegram heavily rate-limits participant queries, so minimizing calls is what
keeps large scans from stalling. Admin lists exclude bots, deleted accounts, the
target, and yourself.

Scanning runs over the **`/ws/scan` WebSocket** to stream live progress. Flood
handling is deliberately **visible**: during a scan the client's
`flood_sleep_threshold` is lowered so `FloodWaitError` is raised (not silently
slept), caught in the scan loop, and reported as a "waiting Ns" progress frame —
otherwise a large scan looks frozen. There's also a plain `POST /api/scan`
fallback with no progress.

### Removal & outreach
Removal (`remove.py`) kicks by default (rejoinable) or bans; it attempts the
action directly and lets Telegram enforce permissions (a pre-check via
`get_permissions` is unreliable on basic groups). Outreach aggregates the
selected `needs_admin` groups **per person** (owner preferred, else all admins,
deduped) so each contact gets one message; the frontend expands an editable
template into per-person drafts, then sends with throttling and flood backoff —
**abort** entirely on `PeerFloodError` (spam limit), **wait** on `FloodWaitError`.

### Builder pipeline (`build.py`)
Two steps, each a WebSocket so Telegram's heavy rate-limiting on these calls
surfaces as visible progress (same `flood_sleep_threshold` trick as the scan):
`/ws/resolve` turns a pasted blob (`parse_identifiers` accepts `@name`, `name`,
`t.me/...`) into resolved users (deduped by id) + an unresolved list with
reasons; `/ws/build` creates a megagroup (`create_supergroup`) or resolves the
chosen existing group(s), then adds people **one at a time** (`add_users`) so
each gets a clean per-user `AddOutcome` (added / needs_invite / already_member /
failed) rather than one privacy-restricted user failing a whole batch. The
existing-group destination is **multi-select**: one build run queues the
recipients through each chosen group in turn (`build_and_add` loops groups,
streaming `plan`/`group_start`/`progress`/`group_done` events and returning a
`results` list). A group-level failure (`abort`) only skips that group; an
account-level spam limit (`PeerFloodError` → `stop_all`) halts the whole run.
Adds are throttled (`_ADD_DELAY`). The group picker loads via `/ws/groups`
(`list_addable_groups` streams `scanned/found` progress since paging all dialogs
is slow) and the frontend caches the result in `localStorage` (`tg_groups_cache`,
7-day TTL, with a Refresh button); a just-created group is appended to the cache
so it appears without a reload. **Gotcha:** on current Telegram layers
`inviteToChannel`/`addChatUser` return `messages.InvitedUsers` and report
privacy/eligibility blocks in `missing_invitees` *instead of raising*
`UserPrivacyRestrictedError` — so `add_user` must inspect the result
(`_missing_invitee_ids`), not just catch exceptions, or blocked users get
silently mislabelled "added" and never offered the link. Anyone bucketed
`needs_invite` is
handled via `export_invite` — the frontend shows the link and reuses the
Remover outreach send-path (`/api/contact/send`, throttled, flood/spam backoff)
to DM it. `list_addable_groups`/`get_contacts` back the two pickers; the session
is persisted after resolve and after build so access-hashes survive a restart.

### Frontend model (`index.html`)
One static file, plain DOM + `fetch`/WebSocket, no framework. State lives in a few
module-level vars (`lastScan`, `outreach`, `activeBucket`). Results render as
tabs (one per bucket) with per-row checkboxes; selection is scoped per bucket so
each tab's action is unambiguous. A 401 from any call drops back to the login
screen; the ToS acknowledgement is remembered in `localStorage` (`tg_ack`).
Group **links** differ by context: `tg://` deep links in the UI (open the app),
`https://t.me/...` in outreach messages (Telegram linkifies these); basic groups
have no linkable URL.
