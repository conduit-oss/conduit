"""Diff a published webhook payload schema.

Providers publish event objects in an API document. The fixture is a trimmed
pair: an ``Event`` schema whose properties are the payload a handler reads.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from conduit.impact.model import ApiChange

_RENAMED = re.compile(r"renamed from\s+([A-Za-z0-9_.]+)", re.IGNORECASE)


def diff_webhook_schemas(old: Path, new: Path, *, provider: str) -> list[ApiChange]:
    previous = _flatten(old)
    latest = _flatten(new)
    if previous is None or latest is None:
        return []
    changes: list[ApiChange] = []
    removed = set(previous) - set(latest)
    renamed_old: set[str] = set()
    for path, description in sorted(latest.items()):
        match = _RENAMED.search(description or "")
        if match is None:
            continue
        origin = match.group(1)
        full = origin if "." in origin else _join(path, origin)
        if full not in removed and origin not in removed:
            continue
        old_path = full if full in removed else origin
        renamed_old.add(old_path)
        changes.append(
            ApiChange(
                kind="field_renamed",
                symbol=old_path,
                old=old_path,
                new=path,
                detail=f"webhook field {old_path} renamed to {path}",
                confidence="medium",
                source="webhook",
                service=provider,
            )
        )
    for path in sorted(removed - renamed_old):
        changes.append(
            ApiChange(
                kind="field_removed",
                symbol=path,
                old=path,
                new="",
                detail=f"webhook field {path} removed",
                confidence="high",
                source="webhook",
                service=provider,
            )
        )
    return changes


def _join(new_path: str, origin: str) -> str:
    if "." not in new_path:
        return origin
    parent = new_path.rsplit(".", 1)[0]
    return f"{parent}.{origin}"


def _flatten(path: Path) -> dict[str, str] | None:
    doc = _load(path)
    if doc is None:
        return None
    if isinstance(doc.get("schemas"), dict) and "components" not in doc:
        return _walk_simple(doc["schemas"].get("Event") or doc["schemas"], "")
    schemas = ((doc.get("components") or {}).get("schemas") or {}) if isinstance(doc.get("components"), dict) else {}
    if not isinstance(schemas, dict) or not schemas:
        return {}
    root_name = "Event" if "Event" in schemas else next(iter(schemas))
    return _walk_openapi(schemas[root_name], schemas, "")


def _walk_simple(node: Any, prefix: str) -> dict[str, str]:
    if not isinstance(node, dict):
        return {}
    props = node.get("properties") if isinstance(node.get("properties"), dict) else node
    found: dict[str, str] = {}
    if not isinstance(props, dict):
        return found
    for key, value in props.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        description = ""
        if isinstance(value, dict):
            description = str(value.get("description") or "")
            nested = value.get("properties")
            if isinstance(nested, dict):
                found.update(_walk_simple(value, path))
                continue
            ref_props = value.get("object")
            if isinstance(ref_props, dict) and "type" not in value and "description" not in value:
                found.update(_walk_simple({"properties": value}, path))
                continue
        found[path] = description
    return found


def _walk_openapi(node: Any, schemas: dict[str, Any], prefix: str, *, stack: tuple[str, ...] = ()) -> dict[str, str]:
    resolved, name = _resolve(node, schemas)
    if name and name in stack:
        return {}
    if not isinstance(resolved, dict):
        return {}
    props = resolved.get("properties")
    if not isinstance(props, dict):
        return {}
    next_stack = stack + ((name,) if name else ())
    found: dict[str, str] = {}
    for key, value in props.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        child, _child_name = _resolve(value, schemas)
        description = ""
        if isinstance(value, dict):
            description = str(value.get("description") or "")
        if isinstance(child, dict) and isinstance(child.get("properties"), dict):
            found.update(_walk_openapi(child, schemas, path, stack=next_stack))
            continue
        if isinstance(child, dict) and not description:
            description = str(child.get("description") or "")
        found[path] = description
    return found


def _resolve(node: Any, schemas: dict[str, Any]) -> tuple[Any, str]:
    if not isinstance(node, dict):
        return node, ""
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        return schemas.get(name), name
    return node, ""


def _load(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None
