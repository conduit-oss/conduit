"""Load producer-authored reshape recipes (declaration ops not derivable from export ids)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ReshapeRecipeError(ValueError):
    """Malformed or mismatched reshape recipe."""


def load_reshape_recipe(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ReshapeRecipeError("recipe must be a JSON object")
    for key in ("package", "from_version", "to_version"):
        if not str(data.get(key) or "").strip():
            raise ReshapeRecipeError(f"recipe missing {key}")
    rules = data.get("rules")
    if rules is None:
        data["rules"] = []
    elif not isinstance(rules, list):
        raise ReshapeRecipeError("recipe.rules must be an array")
    effects = data.get("side_effects")
    if effects is None:
        data["side_effects"] = []
    elif not isinstance(effects, list):
        raise ReshapeRecipeError("recipe.side_effects must be an array")
    return data


def recipe_matches(
    recipe: dict[str, Any],
    *,
    package: str,
    from_version: str,
    to_version: str,
) -> bool:
    return (
        str(recipe.get("package") or "").strip() == package.strip()
        and str(recipe.get("from_version") or "").strip() == from_version.strip()
        and str(recipe.get("to_version") or "").strip() == to_version.strip()
    )


def merge_recipe_into_packet(
    packet: dict[str, Any],
    recipe: dict[str, Any],
) -> dict[str, Any]:
    """Append recipe rules/side_effects; require package/version match."""
    package = str(packet.get("package") or "")
    from_version = str(packet.get("from_version") or "")
    to_version = str(packet.get("to_version") or "")
    if not recipe_matches(
        recipe,
        package=package,
        from_version=from_version,
        to_version=to_version,
    ):
        raise ReshapeRecipeError(
            f"recipe {recipe.get('package')} "
            f"{recipe.get('from_version')}→{recipe.get('to_version')} "
            f"does not match packet {package} {from_version}→{to_version}"
        )
    out = dict(packet)
    rules = list(out.get("rules") or [])
    rules.extend(r for r in (recipe.get("rules") or []) if isinstance(r, dict))
    out["rules"] = rules
    effects = list(out.get("side_effects") or [])
    effects.extend(
        e for e in (recipe.get("side_effects") or []) if isinstance(e, dict)
    )
    out["side_effects"] = effects
    note = str(recipe.get("notes") or "").strip()
    if note:
        prev = str(out.get("notes") or "").strip()
        out["notes"] = f"{prev}\n{note}".strip() if prev else note
    return out
