"""Remint cook: close packet rules into apply-ready bundles (package-neutral)."""

from __future__ import annotations

import re
from typing import Any

# Module path only: a.b.c — no spaces, commas, or import statement fragments.
_MODULE_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


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


def cook_import_member_companions(
    rules: list[dict[str, Any]],
    *,
    package: str,
) -> list[dict[str, Any]]:
    """
    Ensure bare CALL renames have a sibling ``import_member`` declaration.

    ``from pkg import A, old`` stays combined-import safe via the existing
    declaration engine. Module comes from ``package`` when the CALL is a bare
    imported name (decorator / bare callee). Cross-module hops must already
    include an explicit ``import_member`` (not invented here).
    """
    pkg = (package or "").strip()
    if not pkg or not rules:
        return list(rules)

    existing = _existing_import_member_sources(rules)
    extras: list[dict[str, Any]] = []
    for rule in rules:
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        old = _bare_ident(str(rule.get("old_callee") or ""))
        new_raw = str(rule.get("new_callee") or "").strip()
        new = _bare_ident(new_raw) or _bare_ident(new_raw.rsplit(".", 1)[-1])
        if not old or not new or old == new:
            continue
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
                    or f"Companion import_member for bare CALL {old} -> {new}"
                ),
            }
        )
    if not extras:
        return list(rules)
    return list(rules) + extras
