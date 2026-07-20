# tg-tools

A small, self-hosted suite of Telegram utilities you run against your own
account. It currently has two tools:

**Remover** — find the groups you share with a given user and coordinate
removing them: remove them yourself where you have rights, or draft aggregated
requests to the groups' owners/admins where you don't.

**Builder** — create a group (or pick an existing one you can add to) and bulk-add
people: paste usernames in any format and/or multi-select your contacts. Anyone
whose privacy settings block a direct add is offered the group's invite link,
which you can send them as a throttled DM.

## How it works (and its limits)

- Uses your **own Telegram user account** via the MTProto API (Telethon). The
  Bot API can't discover shared groups or message arbitrary users, so it can't
  do this job.
- It only sees groups **you're also in** — there's no way to enumerate every
  group a stranger belongs to. The scope is always your *shared* groups.
- Acting through an unofficial API client and messaging non-contacts carries a
  real risk of your account being rate-limited or banned. Keep usage modest; you
  are responsible for staying within Telegram's Terms of Service.

## Setup

1. **Get API credentials** at <https://my.telegram.org> → *API development
   tools* → create an app → copy your `api_id` and `api_hash`.

2. **Configure.**
   ```bash
   cp .env.example .env
   # edit .env: set TG_API_ID, TG_API_HASH, and a long random TG_SECRET_KEY
   ```
   `TG_SECRET_KEY` encrypts your stored session at rest.

3. **Install & run.**
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cd apps/selfhosted && uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
   Open <http://127.0.0.1:8000>, log in (phone → code → 2FA if enabled), then
   enter a handle and scan.

It's a monorepo — a tenancy-agnostic `core` package plus a self-hosted app. See
[CLAUDE.md](CLAUDE.md) for the architecture.
