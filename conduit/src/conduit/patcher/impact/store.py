"""Persist impact reports and learned impact rules under .conduit/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _conduit_dir(root: Path) -> Path:
    return root.resolve() / ".conduit"


def impact_report_path(root: Path) -> Path:
    return _conduit_dir(root) / "impact_report.json"


def impact_rules_path(root: Path) -> Path:
    return _conduit_dir(root) / "impact_rules.json"


def load_learned_impact_rules(root: Path) -> list[dict[str, Any]]:
    path = impact_rules_path(root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict)]


def save_learned_impact_rules(root: Path, rules: list[dict[str, Any]]) -> None:
    path = impact_rules_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"rules": rules}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_impact_report(root: Path, report: dict[str, Any]) -> None:
    path = impact_report_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
