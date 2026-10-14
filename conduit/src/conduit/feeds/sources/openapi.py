"""Diff two public OpenAPI documents, previous version against latest."""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from conduit.feeds.client import FeedClient
from conduit.feeds.model import FeedHit
from conduit.feeds.sources.base import read_or_empty
from conduit.feeds.targets import WatchTarget

_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)
_RENAMED_FROM = re.compile(r"renamed from\s+(\S+)", re.IGNORECASE)


class OpenAPISource:
    name = "openapi"
    confidence = "medium"

    def collect(self, target: WatchTarget, client: FeedClient) -> list[FeedHit]:
        previous_url = target.feeds.openapi_previous_url
        latest_url = target.feeds.openapi_latest_url
        if not previous_url or not latest_url:
            return []
        previous = read_or_empty(client, previous_url)
        latest = read_or_empty(client, latest_url)
        if not previous or not latest:
            return []
        return diff_openapi(
            previous,
            latest,
            package=target.feeds.pypi or target.package,
            previous_url=previous_url,
            latest_url=latest_url,
            ecosystem="pypi" if target.pypi_version or target.feeds.pypi else "other",
        )


def diff_openapi(
    previous_text: str,
    latest_text: str,
    *,
    package: str,
    previous_url: str,
    latest_url: str,
    ecosystem: str = "pypi",
) -> list[FeedHit]:
    previous = _load(previous_text)
    latest = _load(latest_text)
    if previous is None or latest is None:
        return []
    from_version = str((previous.get("info") or {}).get("version") or "")
    to_version = str((latest.get("info") or {}).get("version") or "")
    old_ops = _operations(previous)
    new_ops = _operations(latest)
    old_keys = set(old_ops)
    new_keys = set(new_ops)
    removed = old_keys - new_keys
    added = new_keys - old_keys
    hits: list[FeedHit] = []
    for key in sorted(removed):
        method, path = key
        partner = _partner(method, path, old_ops[key], added, new_ops)
        if partner is None:
            hits.append(
                _hit(
                    package=package,
                    ecosystem=ecosystem,
                    from_version=from_version,
                    to_version=to_version,
                    change="removed",
                    old=f"{method} {path}",
                    new="",
                    detail=f"OpenAPI removed {method} {path}",
                    url=latest_url,
                )
            )
            continue
        new_method, new_path = partner
        hits.append(
            _hit(
                package=package,
                ecosystem=ecosystem,
                from_version=from_version,
                to_version=to_version,
                change="path_rename",
                old=f"{method} {path}",
                new=f"{new_method} {new_path}",
                detail=f"OpenAPI {method} {path} renamed to {new_method} {new_path}",
                url=latest_url,
            )
        )
        for old_name, new_name in _param_renames(old_ops[key], new_ops[partner]):
            hits.append(
                _hit(
                    package=package,
                    ecosystem=ecosystem,
                    from_version=from_version,
                    to_version=to_version,
                    change="param_rename",
                    old=old_name,
                    new=new_name,
                    detail=(
                        f"OpenAPI parameter {old_name} renamed to {new_name} "
                        f"on {method} {path}"
                    ),
                    url=latest_url,
                    target=f"{new_method} {new_path}",
                )
            )
    for key in sorted(old_keys & new_keys):
        method, path = key
        for old_name, new_name in _param_renames(old_ops[key], new_ops[key]):
            hits.append(
                _hit(
                    package=package,
                    ecosystem=ecosystem,
                    from_version=from_version,
                    to_version=to_version,
                    change="param_rename",
                    old=old_name,
                    new=new_name,
                    detail=f"OpenAPI parameter {old_name} renamed to {new_name} on {method} {path}",
                    url=latest_url,
                    target=f"{method} {path}",
                )
            )
    return hits


def _hit(**kwargs: str) -> FeedHit:
    return FeedHit(source="openapi", confidence="medium", **kwargs)


def _partner(
    method: str,
    path: str,
    old_op: dict[str, str],
    added: set[tuple[str, str]],
    new_ops: dict[tuple[str, str], dict[str, str]],
) -> tuple[str, str] | None:
    same_method = [key for key in added if key[0] == method]
    for key in same_method:
        summary = new_ops[key].get("summary") or ""
        match = _RENAMED_FROM.search(summary)
        if match and match.group(1).rstrip(".,)") == path:
            return key
    old_id = old_op.get("operation_id") or ""
    if old_id:
        for key in same_method:
            if new_ops[key].get("operation_id") == old_id:
                return key
    return None


def _param_renames(old_op: dict[str, str], new_op: dict[str, str]) -> list[tuple[str, str]]:
    old_props = _prop_set(old_op.get("props") or "")
    new_props = _prop_set(new_op.get("props") or "")
    removed = old_props - new_props
    added = new_props - old_props
    if len(removed) == 1 and len(added) == 1:
        return [(next(iter(removed)), next(iter(added)))]
    return []


def _prop_set(raw: str) -> set[str]:
    return {part for part in raw.split(",") if part}


def _operations(doc: dict[str, Any]) -> dict[tuple[str, str], dict[str, str]]:
    paths = doc.get("paths") or {}
    if not isinstance(paths, dict):
        return {}
    found: dict[tuple[str, str], dict[str, str]] = {}
    for raw_path, item in paths.items():
        path = str(raw_path or "").strip()
        if not path or not isinstance(item, dict):
            continue
        for method, op in item.items():
            key = str(method or "").strip().lower()
            if key not in _METHODS or not isinstance(op, dict):
                continue
            found[(key.upper(), path)] = {
                "operation_id": str(op.get("operationId") or ""),
                "summary": str(op.get("summary") or ""),
                "props": ",".join(sorted(_body_props(op))),
            }
    return found


def _body_props(op: dict[str, Any]) -> set[str]:
    props: set[str] = set()
    body = (op.get("requestBody") or {}).get("content") or {}
    if not isinstance(body, dict):
        return props
    for media in body.values():
        if not isinstance(media, dict):
            continue
        schema = media.get("schema") or {}
        if isinstance(schema, dict):
            raw = schema.get("properties") or {}
            if isinstance(raw, dict):
                props.update(str(name) for name in raw)
    return props


def _load(text: str) -> dict[str, Any] | None:
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        return None
    try:
        if raw[:1] in "{[":
            data = json.loads(raw)
        else:
            data = yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return data
