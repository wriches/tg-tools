"""Core exception types, surfaced to the app layer for user-facing messages."""
from __future__ import annotations


class LoginError(Exception):
    """An expected, user-facing problem during the login flow."""


class NotAuthorizedError(Exception):
    """No usable authorized session is available."""
