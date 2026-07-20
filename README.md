# tg-tools

A small, self-hosted suite of Telegram utilities you run against your own
account. It currently has one tool:

**Remover** — find the groups you share with a given user and coordinate
removing them: remove them yourself where you have rights, or draft aggregated
requests to the groups' owners/admins where you don't.

More tools may be added over time; the UI presents each as a tab under
**tg-tools**.

## How it works (and its limits)

- Uses your **own Telegram user account** via the MTProto API (Telethon). The
  Bot API cannot discover shared groups or message arbitrary users, so it can't
  do this job.
- It can only see groups **you are also in** — there is no way to enumerate
  every group a stranger belongs to. The scope is always your *shared* groups.
- Acting through an unofficial API client and (later) messaging non-contacts
  carries a real risk of your account being rate-limited or banned. Keep usage
  modest. You are responsible for using this within Telegram's Terms of Service.

## Setup

1. **Get API credentials.** Log in at <https://my.telegram.org> → *API
   development tools* → create an app → copy your `api_id` and `api_hash`.

2. **Configure.**
   ```bash
   cp .env.example .env
   # edit .env: set TG_API_ID, TG_API_HASH, and a long random TG_SECRET_KEY
   ```
   `TG_SECRET_KEY` encrypts your stored session at rest. If you change it later,
   you'll just need to log in again.

3. **Install dependencies.**
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Run** the self-hosted app.
   ```bash
   cd apps/selfhosted
   uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
   Open <http://127.0.0.1:8000>. Log in with your phone number, the code
   Telegram sends you, and (if enabled) your 2-step verification password. Then
   enter a target handle and scan.

## Layout

This is a monorepo: a shared, tenancy-agnostic `core` package and one app per
deployment target built on top of it.

```
core/tg_tools_core/    # shared — talks to Telegram, no storage/web concerns
  client.py              Telethon client + interactive login primitives
  crypto.py              Fernet encryption helpers
  scan.py                read-only common-groups scan + classification
  models.py              domain models (UserBrief, GroupResult, ScanResult, …)
  exceptions.py          LoginError / NotAuthorizedError

apps/selfhosted/         # single-user, full feature set (this app)
  app/config.py          settings from the repo-root .env
  app/db.py              tiny SQLite key/value store
  app/session_store.py   encrypts + persists the one session
  app/service.py         single-user auth/client orchestration (uses core)
  app/schemas.py         API request/response models
  app/main.py            FastAPI app (auth + scan) + static frontend mount
  frontend/index.html    minimal UI (a full SPA arrives at M4)

apps/public/             # planned: removal-only, multi-tenant, no DMs (see its README)
```

## Roadmap

- **M0–M1 (done):** auth + encrypted session + read-only scan & classification.
- **M2 (done):** removal execution, dry-run/confirm gated, kick-or-ban, with an audit log.
- **M3 (done):** admin resolution (bots/deleted/target/self filtered) + per-admin aggregation preview.
- **M4 (done):** outreach UI — editable template → generated drafts → per-message
  edit/delete → send-all / send-individual with one-time semantics + throttling.
- **M5 (done):** hardening & polish — parallel scan (bounded concurrency, one
  participant query per group) with live progress over a WebSocket, and visible
  flood-wait backoff during scan (shows "waiting Ns…" instead of hanging); flood
  backoff on send (stop on spam-limit, honor flood-waits); ToS-acknowledgement
  gate; error-state polish (401 → login, empty-scan state).
