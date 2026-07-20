"""Persistence for the single authorized session, encrypted at rest."""
from __future__ import annotations

import logging

from tg_tools_core import crypto

from . import db
from .config import get_settings

log = logging.getLogger(__name__)

_SESSION_KEY = "session"


def load() -> str | None:
    """Return the decrypted session string, or None if absent/undecryptable."""
    token = db.get(_SESSION_KEY)
    if not token:
        return None
    session = crypto.decrypt(token, get_settings().secret_key)
    if session is None:
        log.warning("Stored session could not be decrypted (wrong TG_SECRET_KEY?).")
    return session


def save(session_string: str) -> None:
    db.set(_SESSION_KEY, crypto.encrypt(session_string, get_settings().secret_key))


def clear() -> None:
    db.delete(_SESSION_KEY)
