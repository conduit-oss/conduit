"""Validate migration rules before emit/apply (generic guards)."""

from __future__ import annotations

import re

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_valid_python_callee(name: str) -> bool:
    """True when name is a dotted Python/TS callee chain, not a REST path or literal."""
    text = str(name or "").strip()
    if not text or " " in text or text.startswith("/"):
        return False
    if text.startswith("v1/"):
        return False
    parts = text.split(".")
    if not parts:
        return False
    return all(_IDENT.match(part) for part in parts)


def ast_call_rewrite_is_safe(rule: dict) -> bool:
    if str(rule.get("type") or "") != "AST_CALL_REWRITE":
        return True
    old = str(rule.get("old_callee") or "").strip()
    new = str(rule.get("new_callee") or "").strip()
    return is_valid_python_callee(old) and is_valid_python_callee(new)
