"""Version parsing shared by the registry and release sources."""

from __future__ import annotations

import re

from packaging.version import InvalidVersion, Version

_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.]+)?)")


def parse_version(value: str | None) -> Version | None:
    raw = str(value or "").strip().lstrip("vV")
    if not raw:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        match = _VERSION_RE.search(raw)
        if match is None:
            return None
        try:
            return Version(match.group(1))
        except InvalidVersion:
            return None


def latest_stable(versions: list[Version]) -> Version | None:
    if not versions:
        return None
    stable = [item for item in versions if not item.is_prerelease]
    pool = stable or versions
    return max(pool)


def is_newer(latest: Version, installed: str | None) -> bool:
    current = parse_version(installed)
    if current is None:
        return False
    return latest > current


def version_text(value: Version) -> str:
    return str(value)
