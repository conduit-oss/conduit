"""Diff two surface packets into a migration packet draft."""

from __future__ import annotations

from typing import Any

from conduit.export_delta.diff import diff_exports
from conduit.packet.validate import validate_packet


class SurfaceDiffError(ValueError):
    """Surface packets cannot be diffed into a migration hop."""


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

    rem_leaves = sorted(n for n in removed if "." not in n)
    add_leaves = sorted(n for n in added if "." not in n)
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
) -> dict[str, Any]:
    """
    Build a migration packet from two surface packets.

    Mechanical only: DEPENDENCY_BUMP, AST_CALL_REWRITE for heuristic renames,
    structured side_effects for removals without a successor.
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
    _pair_unique_class_methods(added, removed, renamed)

    rules: list[dict[str, Any]] = [
        {
            "type": "DEPENDENCY_BUMP",
            "package": package,
            "from_version": from_version,
            "to_version": to_version,
            "ecosystems": ["pip", "pyproject"] if ecosystem == "pypi" else [ecosystem],
            "reason": f"Pin {package} to {to_version} from surface diff",
        }
    ]
    rules.extend(
        migration_rules_from_renames(renamed, target_files=target_files)
    )

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
            "Aliases kept in both versions are not rewritten."
        ),
        "side_effects": effects,
        "rules": rules,
    }
    errors = validate_packet(packet)
    if errors:
        raise SurfaceDiffError("; ".join(errors))
    return packet
