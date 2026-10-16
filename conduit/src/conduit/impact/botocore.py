"""Diff two service-model documents (service-2.json).

Cloud SDKs ship one JSON model per service. Operation and shape changes
there are the cloud-API diff. Call sites are ``service.operation`` names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from conduit.impact.model import ApiChange


def diff_service_models(old: Path, new: Path, *, service: str) -> list[ApiChange]:
    previous = _load(old)
    latest = _load(new)
    if previous is None or latest is None:
        return []
    service_id = (service or _service_id(latest) or _service_id(previous) or "aws").lower()
    old_ops = _operations(previous)
    new_ops = _operations(latest)
    old_shapes = _shapes(previous)
    new_shapes = _shapes(latest)
    changes: list[ApiChange] = []
    for name in sorted(set(old_ops) - set(new_ops)):
        changes.append(
            ApiChange(
                kind="removed",
                symbol=f"{service_id}.{name}",
                old=name,
                new="",
                detail=f"{service_id}.{name} removed",
                confidence="high",
                source="service-model",
                service=service_id,
            )
        )
    for name in sorted(set(old_ops) & set(new_ops)):
        symbol = f"{service_id}.{name}"
        old_in = _members(old_ops[name], old_shapes, "input")
        new_in = _members(new_ops[name], new_shapes, "input")
        for member in sorted(set(old_in) - set(new_in)):
            changes.append(
                ApiChange(
                    kind="param_removed",
                    symbol=symbol,
                    old=member,
                    new="",
                    detail=f"{service_id}.{name} parameter {member} removed",
                    confidence="high",
                    source="service-model",
                    service=service_id,
                )
            )
        old_out = _members(old_ops[name], old_shapes, "output")
        new_out = _members(new_ops[name], new_shapes, "output")
        for member in sorted(set(old_out) - set(new_out)):
            changes.append(
                ApiChange(
                    kind="field_removed",
                    symbol=symbol,
                    old=member,
                    new="",
                    detail=f"{service_id}.{name} response field {member} removed",
                    confidence="high",
                    source="service-model",
                    service=service_id,
                )
            )
    return changes


def operation_matches(sdk_method: str, service: str, operation: str) -> bool:
    """``s3.put_object`` matches service ``s3`` operation ``PutObject``."""
    if "." not in (sdk_method or ""):
        return False
    head, _, tail = sdk_method.partition(".")
    if head.lower() != (service or "").lower():
        return False
    return _snake(operation) == tail.lower() or tail.lower() == operation.lower()


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name or "").lower()


def _load(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _service_id(model: dict[str, Any]) -> str:
    meta = model.get("metadata")
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("endpointPrefix") or meta.get("serviceId") or "")


def _operations(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = model.get("operations")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _shapes(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = model.get("shapes")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _members(operation: dict[str, Any], shapes: dict[str, dict[str, Any]], side: str) -> set[str]:
    ref = operation.get(side)
    if not isinstance(ref, dict):
        return set()
    shape_name = str(ref.get("shape") or "")
    shape = shapes.get(shape_name) or {}
    members = shape.get("members")
    if not isinstance(members, dict):
        return set()
    return {str(name) for name in members}
