"""Rename API/config keys (quoted dict/JSON/YAML keys and .env prefixes)."""

from __future__ import annotations

import re
from pathlib import Path

from conduit.patcher.string_replace import exact_replace
from conduit.prune.grep_imports import SKIP_DIRS

CONFIG_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".ini"}


def is_env_file(path: Path) -> bool:
    return path.name.startswith(".env")


def iter_config_files(root: Path) -> list[Path]:
    """Config/env files even when import-prune dropped them."""
    root = root.resolve()
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if is_env_file(path) or path.suffix.lower() in CONFIG_SUFFIXES:
            out.append(path.resolve())
    return out


def _env_key_variants(old_key: str) -> list[str]:
    snake = old_key.replace("-", "_")
    variants: list[str] = []
    seen: set[str] = set()
    for item in (old_key, snake, snake.upper(), old_key.upper()):
        if item and item not in seen:
            seen.add(item)
            variants.append(item)
    return variants


def _env_new_key(variant: str, new_key: str) -> str:
    snake = new_key.replace("-", "_")
    if variant.isupper():
        return snake.upper()
    if "-" in variant and "_" not in variant:
        return new_key.replace("_", "-")
    return snake if "_" in variant or variant == variant.lower() else new_key


def apply_key_rename(
    content: str,
    old_key: str,
    new_key: str,
    *,
    env_file: bool = False,
) -> tuple[str, int]:
    """Rewrite quoted keys; on env files also rewrite ``KEY=`` line prefixes."""
    if not old_key or not new_key or old_key == new_key:
        return content, 0
    updated = content
    count = 0
    if env_file:
        for variant in _env_key_variants(old_key):
            replacement = _env_new_key(variant, new_key)
            pattern = re.compile(
                rf"(?m)^(\s*(?:export\s+)?){re.escape(variant)}="
            )
            updated, n = pattern.subn(rf"\1{replacement}=", updated)
            count += n
    for quote in ('"', "'"):
        updated, n = exact_replace(
            updated, f"{quote}{old_key}{quote}", f"{quote}{new_key}{quote}"
        )
        count += n
    return updated, count
