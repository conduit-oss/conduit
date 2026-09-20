"""Remint cook: close packet rules into apply-ready bundles (package-neutral)."""

from __future__ import annotations

import re
from typing import Any

# Module path only: a.b.c — no spaces, commas, or import statement fragments.
_MODULE_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_ORM_MODE_RENAME = re.compile(
    r"orm_mode\s*(?:→|->|⇒|to|=)\s*from_attributes",
    re.IGNORECASE,
)


def is_module_path_import(value: str) -> bool:
    """True when ``value`` is a dotted module path suitable for AST_IMPORT_REWRITE."""
    text = (value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if " " in text or "," in text:
        return False
    if lowered.startswith("from ") or " import " in lowered:
        return False
    return bool(_MODULE_PATH.match(text))


def _bare_ident(value: str) -> str | None:
    text = (value or "").strip()
    if not text or "." in text or not text.isidentifier():
        return None
    return text


def _import_member_key(module: str, name: str) -> str:
    return f"{module}::{name}"


def _existing_import_member_sources(rules: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for rule in rules:
        if str(rule.get("type") or "") != "AST_DECLARATION_REWRITE":
            continue
        op = rule.get("operation")
        if not isinstance(op, dict) or op.get("kind") != "import_member":
            continue
        source = op.get("source")
        if not isinstance(source, dict):
            continue
        module = str(source.get("module") or "").strip()
        name = str(source.get("name") or "").strip()
        if module and name:
            out.add(_import_member_key(module, name))
    return out


def _package_local_callable_rename(
    old_callee: str,
    new_callee: str,
    *,
    package: str,
) -> tuple[str, str] | None:
    """
    Map CALL callees to an importable (old_name, new_name) under ``package``.

    Accepts bare names (``validator``) and package-qualified leaves
    (``pydantic.validator`` → ``pydantic.field_validator``).
    """
    pkg = (package or "").strip()
    old = (old_callee or "").strip()
    new = (new_callee or "").strip()
    if not pkg or not old or not new:
        return None

    old_bare = _bare_ident(old)
    new_bare = _bare_ident(new) or _bare_ident(new.rsplit(".", 1)[-1])
    if old_bare and new_bare and old_bare != new_bare:
        return old_bare, new_bare

    prefix = f"{pkg}."
    if not old.startswith(prefix):
        return None
    old_rest = old[len(prefix) :]
    if not old_rest.isidentifier():
        return None
    if new.startswith(prefix):
        new_rest = new[len(prefix) :]
        if new_rest.isidentifier() and new_rest != old_rest:
            return old_rest, new_rest
    if new_bare and new_bare != old_rest:
        return old_rest, new_bare
    return None


def cook_import_member_companions(
    rules: list[dict[str, Any]],
    *,
    package: str,
) -> list[dict[str, Any]]:
    """
    Ensure package-local CALL renames have a sibling ``import_member`` declaration.

    ``from pkg import A, old`` stays combined-import safe via the existing
    declaration engine. Module comes from ``package`` for bare or
    ``{package}.{name}`` callees. Cross-module hops must already include an
    explicit ``import_member`` (not invented here).
    """
    pkg = (package or "").strip()
    if not pkg or not rules:
        return list(rules)

    existing = _existing_import_member_sources(rules)
    extras: list[dict[str, Any]] = []
    for rule in rules:
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        pair = _package_local_callable_rename(
            str(rule.get("old_callee") or ""),
            str(rule.get("new_callee") or ""),
            package=pkg,
        )
        if pair is None:
            continue
        old, new = pair
        key = _import_member_key(pkg, old)
        if key in existing:
            continue
        existing.add(key)
        targets = rule.get("target_files")
        if not isinstance(targets, list) or not targets:
            targets = ["*.py"]
        extras.append(
            {
                "type": "AST_DECLARATION_REWRITE",
                "target_files": list(targets),
                "operation": {
                    "kind": "import_member",
                    "source": {"module": pkg, "name": old},
                    "target": {"module": pkg, "name": new},
                    "local_binding": "preserve",
                },
                "reason": (
                    rule.get("reason")
                    or f"Companion import_member for CALL {old} -> {new}"
                ),
            }
        )
    if not extras:
        return list(rules)
    return list(rules) + extras


def _has_config_inner_class_decl(rules: list[dict[str, Any]]) -> bool:
    for rule in rules:
        if str(rule.get("type") or "") != "AST_DECLARATION_REWRITE":
            continue
        op = rule.get("operation")
        if not isinstance(op, dict) or op.get("kind") != "inner_class_to_assignment":
            continue
        selector = op.get("selector") if isinstance(op.get("selector"), dict) else {}
        if str(selector.get("inner_name") or "") == "Config":
            return True
    return False


def cook_config_from_side_effects(
    rules: list[dict[str, Any]],
    side_effects: Any,
    *,
    package: str,
) -> list[dict[str, Any]]:
    """
    When side_effects name ``orm_mode → from_attributes`` and no Config
    declaration exists, emit ``inner_class_to_assignment`` from that rename.
    """
    pkg = (package or "").strip()
    if not pkg or _has_config_inner_class_decl(rules):
        return list(rules)
    parts: list[str] = []
    if isinstance(side_effects, list):
        for effect in side_effects:
            if isinstance(effect, str):
                parts.append(effect)
            elif isinstance(effect, dict):
                parts.append(str(effect.get("detail") or ""))
    blob = " ".join(parts)
    if not _ORM_MODE_RENAME.search(blob):
        return list(rules)
    if "config" not in blob.lower():
        return list(rules)

    emit: dict[str, Any] = {
        "target": "model_config",
        "constructor": "ConfigDict",
        "ensure_import": {"module": pkg, "name": "ConfigDict"},
    }
    return list(rules) + [
        {
            "type": "AST_DECLARATION_REWRITE",
            "target_files": ["*.py"],
            "operation": {
                "kind": "inner_class_to_assignment",
                "selector": {"inner_name": "Config", "parent_bases_any": []},
                "keys": {"orm_mode": "from_attributes"},
                "emit": emit,
                "unmapped_assignments": "refuse",
                "unsupported_members": "refuse",
            },
            "reason": (
                "Cooked Config declaration from side_effects orm_mode→from_attributes"
            ),
        }
    ]


def cook_demote_package_leaf_calls(
    rules: list[dict[str, Any]],
    *,
    package: str,
) -> list[dict[str, Any]]:
    """
    Rewrite ``{package}.{name}`` CALL leaves to bare ``name``.

    Decorators and combined-import sites observe the imported leaf (``validator``),
    not the qualified ``pydantic.validator`` spelling. Demoting keeps CALL +
    ``import_member`` aligned with Watch leftovers.
    """
    pkg = (package or "").strip()
    if not pkg or not rules:
        return list(rules)
    out: list[dict[str, Any]] = []
    for rule in rules:
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            out.append(rule)
            continue
        pair = _package_local_callable_rename(
            str(rule.get("old_callee") or ""),
            str(rule.get("new_callee") or ""),
            package=pkg,
        )
        old = str(rule.get("old_callee") or "").strip()
        if pair is None or not old.startswith(f"{pkg}."):
            out.append(rule)
            continue
        old_leaf, new_leaf = pair
        updated = dict(rule)
        updated["old_callee"] = old_leaf
        updated["new_callee"] = new_leaf
        # Stale surface export_path would keep the qualified form; drop and
        # let mint floor re-attach on the next normalize pass when present.
        if "surface" in updated:
            updated.pop("surface", None)
        out.append(updated)
    return out


def cook_packet_rules(
    rules: list[dict[str, Any]],
    *,
    package: str,
    side_effects: Any = None,
) -> list[dict[str, Any]]:
    """Run all remint cooks (leaf demote, import companions, Config)."""
    from conduit.surface.mint_surface import enrich_minted_rules

    demoted = cook_demote_package_leaf_calls(rules, package=package)
    # Re-attach surface floor after demoting qualified leaves to bare names.
    demoted = enrich_minted_rules(demoted)
    with_imports = cook_import_member_companions(demoted, package=package)
    return cook_config_from_side_effects(with_imports, side_effects, package=package)
