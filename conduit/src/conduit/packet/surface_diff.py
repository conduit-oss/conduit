"""Diff two surface packets into a migration packet draft."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conduit.export_delta.diff import diff_exports
from conduit.packet.cook import cook_import_member_companions
from conduit.packet.reshape_recipes import (
    ReshapeRecipeError,
    load_reshape_recipe,
    merge_recipe_into_packet,
)
from conduit.packet.validate import validate_packet


class SurfaceDiffError(ValueError):
    """Surface packets cannot be diffed into a migration hop."""


# Generic leaf successor pairs when both legacy and successor appear on the new surface.
_SUCCESSOR_LEAF_PAIRS: tuple[tuple[str, str], ...] = (
    ("dict", "model_dump"),
    ("parse_obj", "model_validate"),
    ("parse_raw", "model_validate_json"),
    ("json", "model_dump_json"),
    ("validator", "field_validator"),
    ("root_validator", "model_validator"),
)

# Decorator successors that need @classmethod on the rewritten defs.
_CLASSMETHOD_DECORATORS = frozenset({"field_validator", "model_validator"})


def _infer_supersessions(
    old_ids: set[str],
    new_ids: set[str],
    renamed: dict[str, str],
) -> None:
    """
    Emit rewrites when the new version keeps a legacy export and adds a successor.

    Example: BaseModel.dict and BaseModel.model_dump both on v2 → hop dict→model_dump.
    """
    for old_leaf, new_leaf in _SUCCESSOR_LEAF_PAIRS:
        new_leaf_present = new_leaf in new_ids or any(
            s.endswith("." + new_leaf) for s in new_ids
        )
        if not new_leaf_present:
            continue

        candidates: list[str] = []
        if old_leaf in old_ids:
            candidates.append(old_leaf)
        candidates.extend(
            s for s in old_ids if "." in s and s.endswith("." + old_leaf)
        )
        for old_path in candidates:
            if old_path in renamed:
                continue
            if "." in old_path:
                prefix = old_path[: -(len(old_leaf) + 1)]
                new_path = f"{prefix}.{new_leaf}"
            else:
                new_path = new_leaf
            if new_path not in new_ids:
                continue
            renamed[old_path] = new_path

def _ids(packet: dict[str, Any]) -> set[str]:
    symbols = packet.get("symbols") or []
    out: set[str] = set()
    for row in symbols:
        if isinstance(row, dict):
            sid = str(row.get("id") or "").strip()
            if sid:
                out.add(sid)
    return out


def _require_surface(packet: dict[str, Any], label: str) -> None:
    if not isinstance(packet, dict):
        raise SurfaceDiffError(f"{label} must be an object")
    if packet.get("packet_kind") != "surface":
        raise SurfaceDiffError(f"{label} must have packet_kind=surface")
    for key in ("package", "ecosystem", "version"):
        if not str(packet.get(key) or "").strip():
            raise SurfaceDiffError(f"{label} missing {key}")


def _pair_unique_class_methods(
    added: set[str],
    removed: set[str],
    renamed: dict[str, str],
) -> None:
    """
    When a class loses exactly one public method and gains exactly one,
    treat that as a rename (Client.run → Client.execute).
    Also pair unique top-level leaf removals/additions (run → execute).
    """
    from collections import defaultdict

    rem_by: dict[str, list[str]] = defaultdict(list)
    add_by: dict[str, list[str]] = defaultdict(list)
    for name in removed:
        if "." not in name:
            continue
        cls, _meth = name.rsplit(".", 1)
        rem_by[cls].append(name)
    for name in added:
        if "." not in name:
            continue
        cls, _meth = name.rsplit(".", 1)
        add_by[cls].append(name)
    for cls, olds in rem_by.items():
        news = add_by.get(cls) or []
        if len(olds) != 1 or len(news) != 1:
            continue
        old, new = olds[0], news[0]
        if old in renamed:
            continue
        renamed[old] = new
        removed.discard(old)
        added.discard(new)

    rem_leaves = sorted(n for n in removed if "." not in n and "/" not in n)
    add_leaves = sorted(n for n in added if "." not in n and "/" not in n)
    if len(rem_leaves) == 1 and len(add_leaves) == 1:
        old, new = rem_leaves[0], add_leaves[0]
        if old not in renamed:
            renamed[old] = new
            removed.discard(old)
            added.discard(new)


def migration_rules_from_renames(
    renamed: dict[str, str],
    *,
    target_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Map confident renames onto AST_CALL_REWRITE (leaf or Class.member)."""
    targets = target_files or ["*.py"]
    rules: list[dict[str, Any]] = []
    for old, new in sorted(renamed.items()):
        if not old or not new or old == new:
            continue
        rules.append(
            {
                "type": "AST_CALL_REWRITE",
                "target_files": list(targets),
                "old_callee": old,
                "new_callee": new,
                "reason": f"surface rename {old} → {new}",
            }
        )
    return rules


def _ensure_classmethod_companions(
    rules: list[dict[str, Any]],
    *,
    target_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Add ensure_classmethod when CALL rewrites land on known decorator successors."""
    targets = target_files or ["*.py"]
    seen: set[str] = set()
    extras: list[dict[str, Any]] = []
    for rule in rules:
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        new = str(rule.get("new_callee") or "").strip()
        leaf = new.split(".")[-1] if new else ""
        if leaf not in _CLASSMETHOD_DECORATORS or leaf in seen:
            continue
        seen.add(leaf)
        extras.append(
            {
                "type": "AST_DECLARATION_REWRITE",
                "target_files": list(targets),
                "operation": {"kind": "ensure_classmethod", "decorator": leaf},
                "reason": f"Companion ensure_classmethod for decorator {leaf}",
            }
        )
    if not extras:
        return list(rules)
    return list(rules) + extras


def side_effects_for_removed(
    removed: set[str],
    *,
    evidence_from: str | None = None,
    evidence_to: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Removed exports with no rename become structured gaps."""
    effects: list[dict[str, Any]] = []
    for old in sorted(removed)[:limit]:
        row: dict[str, Any] = {
            "kind": "other",
            "detail": f"Public export removed with no rename match: {old}",
            "gap_kind": "uncodable",
            "old_shape": old,
            "blocker": "surface diff found no successor id",
        }
        if evidence_from:
            row["evidence_url"] = evidence_from
        elif evidence_to:
            row["evidence_url"] = evidence_to
        effects.append(row)
    return effects


def diff_surface_packets(
    surface_from: dict[str, Any],
    surface_to: dict[str, Any],
    *,
    target_files: list[str] | None = None,
    recipe: dict[str, Any] | None = None,
    recipe_path: Path | None = None,
) -> dict[str, Any]:
    """
    Build a migration packet from two surface packets.

    Mechanical only: DEPENDENCY_BUMP, AST_CALL_REWRITE for heuristic renames,
    structured side_effects for removals without a successor.
    Optional reshape recipes add declaration ops that export ids cannot prove
    (e.g. nested Config → model_config).
    """
    _require_surface(surface_from, "from")
    _require_surface(surface_to, "to")
    package = str(surface_from.get("package") or "").strip()
    if package != str(surface_to.get("package") or "").strip():
        raise SurfaceDiffError("package mismatch between surface packets")
    ecosystem = str(surface_from.get("ecosystem") or "pypi").strip().lower() or "pypi"
    from_version = str(surface_from.get("version") or "").strip()
    to_version = str(surface_to.get("version") or "").strip()
    if not from_version or not to_version:
        raise SurfaceDiffError("both surface packets need version")
    if from_version == to_version:
        raise SurfaceDiffError("from and to versions must differ")

    old_ids = _ids(surface_from)
    new_ids = _ids(surface_to)
    added, removed, renamed = diff_exports(old_ids, new_ids)
    _infer_supersessions(old_ids, new_ids, renamed)
    _pair_unique_class_methods(added, removed, renamed)

    dep_bump: dict[str, Any] = {
        "type": "DEPENDENCY_BUMP",
        "package": package,
        "from_version": from_version,
        "to_version": to_version,
        "reason": f"Pin {package} to {to_version} from surface diff",
    }
    # dep_ecosystems schema has no "other"; omit when surface ecosystem is not a lockfile kind.
    _ECO_TO_DEP = {
        "pypi": ["pip", "pyproject"],
        "npm": ["npm"],
        "go": ["go"],
        "maven": ["maven", "gradle"],
    }
    if ecosystem in _ECO_TO_DEP:
        dep_bump["ecosystems"] = list(_ECO_TO_DEP[ecosystem])
    rules: list[dict[str, Any]] = [dep_bump]
    rules.extend(
        migration_rules_from_renames(renamed, target_files=target_files)
    )
    rules = cook_import_member_companions(rules, package=package)
    rules = _ensure_classmethod_companions(rules, target_files=target_files)

    evidence = (
        f"surface:{ecosystem}:{package}:{from_version}"
        f"→{to_version}"
    )
    effects = side_effects_for_removed(
        removed,
        evidence_from=evidence,
    )

    packet: dict[str, Any] = {
        "packet_id": f"{package}-{from_version}-{to_version}",
        "package": package,
        "ecosystem": ecosystem,
        "from_version": from_version,
        "to_version": to_version,
        "sources": [
            {
                "url": f"conduit://surface/{ecosystem}/{package}/{from_version}",
                "kind": "other",
            },
            {
                "url": f"conduit://surface/{ecosystem}/{package}/{to_version}",
                "kind": "other",
            },
        ],
        "notes": (
            "Draft hop from surface_packet diff. "
            f"renamed={len(renamed)} removed={len(removed)} added={len(added)}. "
            "Includes supersession rewrites, import_member companions, and "
            "ensure_classmethod for decorator successors."
        ),
        "side_effects": effects,
        "rules": rules,
    }

    recipe_data = recipe
    if recipe_data is None and recipe_path is not None:
        try:
            recipe_data = load_reshape_recipe(Path(recipe_path))
        except (OSError, json.JSONDecodeError, ReshapeRecipeError) as exc:
            raise SurfaceDiffError(f"recipe load failed: {exc}") from exc
    if recipe_data is not None:
        try:
            packet = merge_recipe_into_packet(packet, recipe_data)
        except ReshapeRecipeError as exc:
            raise SurfaceDiffError(str(exc)) from exc

    errors = validate_packet(packet)
    if errors:
        raise SurfaceDiffError("; ".join(errors))
    return packet
