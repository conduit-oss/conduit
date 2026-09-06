"""Persist learned post-rules under .conduit/."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_POST_RULES_NAME = "post_rules.json"
_META_NAME = "post_rules_meta.json"


def _conduit_dir(root: Path) -> Path:
    return root.resolve() / ".conduit"


def post_rules_path(root: Path) -> Path:
    return _conduit_dir(root) / _POST_RULES_NAME


def post_rules_meta_path(root: Path) -> Path:
    return _conduit_dir(root) / _META_NAME


def _rule_key(rule: dict[str, Any]) -> tuple[Any, ...]:
    return (
        rule.get("type"),
        tuple(rule.get("target_files") or ()),
        rule.get("function_name"),
        rule.get("delegate_symbol"),
        rule.get("required_key"),
        rule.get("list_callee_hint"),
        rule.get("body"),
        rule.get("match"),
        rule.get("replace"),
    )


def load_learned_post_rules(root: Path) -> list[dict[str, Any]]:
    path = post_rules_path(root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict)]


def save_learned_post_rules(
    root: Path,
    rules: list[dict[str, Any]],
    *,
    failure_fingerprint: str | None = None,
) -> None:
    root = root.resolve()
    _conduit_dir(root).mkdir(parents=True, exist_ok=True)
    payload = {"rules": rules}
    post_rules_path(root).write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    if failure_fingerprint:
        meta_path = post_rules_meta_path(root)
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        fps = list(meta.get("fingerprints") or [])
        if failure_fingerprint not in fps:
            fps.append(failure_fingerprint)
        meta["fingerprints"] = fps[-50:]
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def merge_post_rules(
    *rule_lists: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for rules in rule_lists:
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            key = _rule_key(rule)
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(rule))
    return out


def packet_post_rules(packet: dict[str, Any]) -> list[dict[str, Any]]:
    raw = packet.get("post_rules") or []
    return [r for r in raw if isinstance(r, dict)]


def export_post_rules_to_packet(
    packet: dict[str, Any],
    learned: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a copy of packet with learned post_rules merged (no duplicates)."""
    out = dict(packet)
    merged = merge_post_rules(packet_post_rules(packet), learned)
    if merged:
        out["post_rules"] = merged
    return out


def failure_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
