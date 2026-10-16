"""errors (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations


class SyncError(Exception):
    """Expected operational error with a safe, non-secret message."""


class InvalidInput(SyncError):
    pass


class ProbeError(SyncError):
    pass


class TransactionError(SyncError):
    pass
