"""Shared, tenancy-agnostic core for tg-tools.

Contains everything that talks to Telegram (client construction, the interactive
login primitives, the read-only common-groups scan, and domain models). It is
deliberately free of any storage / multi-tenancy / web concerns so that both the
self-hosted app and the public removal-only app can build on top of it.
"""

__version__ = "0.1.0"
