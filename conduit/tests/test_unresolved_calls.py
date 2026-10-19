"""Unresolved attribute calls must survive map filtering and appear in map JSON."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.outside_in.analyze import analyze_repo


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


def test_unresolved_calls_survive_unreached_filter_and_map_json(tmp_path: Path):
    # A real outside-service import so the map keeps services; plus an untyped
    # attribute call that cannot be resolved.
    _write(
        tmp_path,
        "app.py",
        """
        import socket

        def ping(host):
            return socket.create_connection((host, 80))

        def mystery(obj):
            return obj.do_thing()
        """,
    )
    service_map = analyze_repo(tmp_path)
    assert service_map.unresolved_calls, "expected at least one unresolved attribute call"
    reasons = service_map.unresolved_by_reason()
    assert sum(reasons.values()) == len(service_map.unresolved_calls)

    runner = CliRunner()
    result = runner.invoke(app, ["map", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    unresolved = payload.get("unresolved_calls") or {}
    assert unresolved.get("total", 0) >= 1
    assert unresolved.get("by_reason")
