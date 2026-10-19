"""Fails before the fix (object_), passes after (statement)."""

from __future__ import annotations

import sys
import types


def _install_stub() -> None:
    """Minimal stub so tests run without the real package installed."""
    if "sqlalchemy" in sys.modules:
        return
    engine = types.ModuleType("sqlalchemy.engine")

    class Connection:  # noqa: N801 — mirrors the imported name
        pass

    engine.Connection = Connection
    pkg = types.ModuleType("sqlalchemy")
    pkg.engine = engine
    sys.modules["sqlalchemy"] = pkg
    sys.modules["sqlalchemy.engine"] = engine


_install_stub()

from db import fetch_one  # noqa: E402


class _FakeConn:
    def scalar(self, **kwargs):
        if "object_" in kwargs:
            raise TypeError("scalar() got an unexpected keyword argument 'object_'")
        return kwargs.get("statement")


def test_fetch_one_uses_current_keyword():
    assert fetch_one(_FakeConn(), "SELECT 1") == "SELECT 1"
