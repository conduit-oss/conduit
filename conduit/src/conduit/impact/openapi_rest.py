"""Diff two OpenAPI documents for removed or renamed operations and fields.

The documents are published specs (a fixture pair, or a file fetched from a
public spec repository). This module does not ship a list of endpoint changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from conduit.impact.model import ApiChange

#: Latest documents in public spec repositories. A single file is not a diff.
PUBLIC_SPEC_URLS = {
    "stripe": "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json",
    "github": (
        "https://raw.githubusercontent.com/github/rest-api-description/main/"
        "descriptions/api.github.com/api.github.com.json"
    ),
    "openai": "https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml",
}


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    operation_id: str
    request_fields: tuple[str, ...]
    response_fields: tuple[str, ...]


def diff_openapi(old: Path, new: Path, *, service: str) -> list[ApiChange]:
    """Removed and renamed operations, plus request and response fields."""
    before = load_openapi(old)
    after = load_openapi(new)
    if before is None or after is None:
        return []
    old_ops = operations(before)
    new_ops = operations(after)
    new_keys = {(item.method, item.path) for item in new_ops}
    old_by_id = {item.operation_id: item for item in old_ops if item.operation_id}
    new_by_id = {item.operation_id: item for item in new_ops if item.operation_id}
    new_by_key = {(item.method, item.path): item for item in new_ops}
    changes: list[ApiChange] = []
    renamed: set[tuple[str, str]] = set()
    for op_id, previous in sorted(old_by_id.items()):
        current = new_by_id.get(op_id)
        if current is None or (current.method, current.path) == (previous.method, previous.path):
            continue
        renamed.add((previous.method, previous.path))
        changes.append(
            ApiChange(
                kind="endpoint_renamed",
                symbol=f"{previous.method} {previous.path}",
                old=previous.path,
                new=current.path,
                detail=f"{previous.method} {previous.path} renamed to {current.method} {current.path}",
                confidence="high",
                source="openapi",
                service=service,
            )
        )
    for previous in old_ops:
        key = (previous.method, previous.path)
        if key in renamed or key in new_keys or _template_present(key, new_keys):
            continue
        changes.append(
            ApiChange(
                kind="endpoint_removed",
                symbol=f"{previous.method} {previous.path}",
                old=previous.path,
                new="",
                detail=f"{previous.method} {previous.path} removed",
                confidence="high",
                source="openapi",
                service=service,
            )
        )
    for previous in old_ops:
        key = (previous.method, previous.path)
        current = new_by_key.get(key)
        if current is None:
            current = _template_partner(key, new_by_key)
        if current is None:
            continue
        changes.extend(_field_changes(previous, current, service=service))
    return changes


def load_openapi(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        if path.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.safe_load(text)
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def operations(document: dict[str, Any]) -> list[Operation]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return []
    found: list[Operation] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters") if isinstance(item.get("parameters"), list) else []
        for method, operation in item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if not isinstance(operation, dict):
                continue
            params = list(shared_params)
            own = operation.get("parameters")
            if isinstance(own, list):
                params.extend(own)
            found.append(
                Operation(
                    method=method.upper(),
                    path=str(path),
                    operation_id=str(operation.get("operationId") or ""),
                    request_fields=tuple(
                        sorted(
                            {
                                *_parameter_names(params, document),
                                *_schema_fields(_request_schema(operation), document),
                            }
                        )
                    ),
                    response_fields=tuple(sorted(_schema_fields(_response_schema(operation), document))),
                )
            )
    return found


def endpoint_symbol(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def split_http_target(sdk_method: str) -> tuple[str, str]:
    """``POST https://host/v1/charges`` -> ``(POST, /v1/charges)``."""
    text = (sdk_method or "").strip()
    method = ""
    url = text
    pieces = text.split(" ", 1)
    if len(pieces) == 2 and pieces[0].isalpha() and pieces[0].isupper():
        method, url = pieces
    parsed = urlparse(url)
    path = parsed.path or ""
    if not path and url.startswith("/"):
        path = url.split("?", 1)[0]
    return method, path


def paths_match(left: str, right: str) -> bool:
    a = [part for part in (left or "").split("/") if part]
    b = [part for part in (right or "").split("/") if part]
    if len(a) != len(b):
        return False
    for one, other in zip(a, b):
        if one == other or _is_param(one) or _is_param(other):
            continue
        return False
    return True


def _field_changes(previous: Operation, current: Operation, *, service: str) -> list[ApiChange]:
    changes: list[ApiChange] = []
    symbol = endpoint_symbol(previous.method, previous.path)
    for name in sorted(set(previous.request_fields) - set(current.request_fields)):
        changes.append(
            ApiChange(
                kind="field_removed",
                symbol=symbol,
                old=name,
                new="",
                detail=f"request field {name} removed from {previous.method} {previous.path}",
                confidence="high",
                source="openapi",
                service=service,
            )
        )
    for name in sorted(set(previous.response_fields) - set(current.response_fields)):
        changes.append(
            ApiChange(
                kind="field_removed",
                symbol=symbol,
                old=name,
                new="",
                detail=f"response field {name} removed from {previous.method} {previous.path}",
                confidence="high",
                source="openapi",
                service=service,
            )
        )
    return changes


def _template_present(key: tuple[str, str], keys: set[tuple[str, str]]) -> bool:
    method, path = key
    return any(other_method == method and paths_match(path, other_path) for other_method, other_path in keys)


def _template_partner(
    key: tuple[str, str], index: dict[tuple[str, str], Operation]
) -> Operation | None:
    method, path = key
    for (other_method, other_path), operation in index.items():
        if other_method == method and paths_match(path, other_path):
            return operation
    return None


def _is_param(part: str) -> bool:
    return part in {"{}", "*"} or (part.startswith("{") and part.endswith("}")) or part.startswith(":")


def _parameter_names(params: list[Any], document: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for item in params:
        if isinstance(item, dict) and "$ref" in item:
            item = _resolve_ref(str(item.get("$ref") or ""), document) or {}
        if isinstance(item, dict) and item.get("name"):
            found.add(str(item["name"]))
    return found


def _request_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    if not isinstance(content, dict):
        return None
    for value in content.values():
        if isinstance(value, dict) and isinstance(value.get("schema"), dict):
            return value["schema"]
    return None


def _response_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    preferred = responses.get("200") or responses.get("201") or responses.get("default")
    if not isinstance(preferred, dict):
        for value in responses.values():
            if isinstance(value, dict):
                preferred = value
                break
    if not isinstance(preferred, dict):
        return None
    content = preferred.get("content")
    if not isinstance(content, dict):
        return None
    for value in content.values():
        if isinstance(value, dict) and isinstance(value.get("schema"), dict):
            return value["schema"]
    return None


def _schema_fields(schema: dict[str, Any] | None, document: dict[str, Any], depth: int = 0) -> set[str]:
    if not isinstance(schema, dict) or depth > 4:
        return set()
    if "$ref" in schema:
        resolved = _resolve_ref(str(schema.get("$ref") or ""), document)
        return _schema_fields(resolved, document, depth + 1)
    found: set[str] = set()
    props = schema.get("properties")
    if isinstance(props, dict):
        found.update(str(name) for name in props)
    for key in ("allOf", "oneOf", "anyOf"):
        items = schema.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    found.update(_schema_fields(item, document, depth + 1))
    return found


def _resolve_ref(ref: str, document: dict[str, Any]) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    node: Any = document
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node if isinstance(node, dict) else None
