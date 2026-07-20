# tg-tools — public app (planned)

A **removal-only**, multi-tenant web app intended for public deployment. It is a
deliberately limited subset of the self-hosted app:

- ✅ Connect a Telegram account, enter a handle, scan common groups, and remove
  the target from groups where you already have removal rights.
- ❌ No admin/owner resolution and **no DMing** anyone — those carry the real
  account-ban risk and are reserved for the self-hosted app.

It will be built on the shared `core/` package (`tg_tools_core`): the same
client, login primitives, scan, and (M2) removal logic, with app-specific
concerns layered on top:

- **Multi-tenancy** — per-user isolation instead of one local session.
- **Ephemeral sessions** — log in → scan → remove → discard the session; never
  persist session strings long-term, to minimize breach blast radius.
- **Abuse guardrails & rate limiting** — appropriate for an open deployment.
- **App-level auth + ToS acknowledgement.**

> Not yet implemented. The monorepo structure and shared core are in place so
> this can be added without duplicating Telegram logic.
