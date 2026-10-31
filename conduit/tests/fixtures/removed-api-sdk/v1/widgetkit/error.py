"""Errors removed in v2 — tests that import this break collection after the bump."""


class GoneError(Exception):
    """Raised when a widget lookup fails."""
