"""Prefer intermediate package/SDK version steps over jumping to latest.

Vendor modules should bump one major at a time (default), or the next
published release when majors_only is False.
"""

from __future__ import annotations

import re
from typing import Iterable

from packaging.version import InvalidVersion, Version

_TAG_RE = re.compile(r"^v?(?P<version>\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)$")


def parse_release_version(tag: str | Version) -> Version | None:
    """Parse a git tag or version string into a packaging Version."""
    if isinstance(tag, Version):
        return tag
    text = str(tag).strip()
    match = _TAG_RE.match(text)
    raw = match.group("version") if match else text.lstrip("v")
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def list_release_versions(tags: Iterable[str | Version]) -> list[Version]:
    """Return unique stable-sorted Versions from tag strings (drop unparseable)."""
    seen: set[str] = set()
    out: list[Version] = []
    for tag in tags:
        ver = parse_release_version(tag)
        if ver is None:
            continue
        key = str(ver)
        if key in seen:
            continue
        seen.add(key)
        out.append(ver)
    out.sort()
    return out


def next_version_step(
    installed: Version | str,
    versions: Iterable[str | Version],
    *,
    majors_only: bool = True,
) -> Version | None:
    """
    Choose the next migration target version.

    majors_only=True:
      Highest published version on the next major line
      (installed.major + 1), e.g. 0.28 → latest 1.x even if 2.x exists.
    majors_only=False:
      Smallest published version strictly greater than installed.
    """
    installed_v = parse_release_version(installed)
    if installed_v is None:
        return None
    available = list_release_versions(versions)
    if not available:
        return None

    if majors_only:
        target_major = installed_v.major + 1
        on_major = [v for v in available if v.major == target_major and not v.is_prerelease]
        if not on_major:
            # Allow prerelease on next major only if nothing stable exists
            on_major = [v for v in available if v.major == target_major]
        if not on_major:
            return None
        return max(on_major)

    greater = [v for v in available if v > installed_v]
    if not greater:
        return None
    # Prefer non-prerelease when available
    stable = [v for v in greater if not v.is_prerelease]
    return min(stable or greater)


def version_step_reason(
    installed: Version | str,
    chosen: Version | str,
    latest: Version | str | None = None,
    *,
    package: str | None = None,
    majors_only: bool = True,
) -> str:
    """Human-readable rationale for a stepped dependency bump."""
    inst = parse_release_version(installed)
    ch = parse_release_version(chosen)
    lat = parse_release_version(latest) if latest is not None else None
    inst_s = str(inst.base_version) if inst else str(installed)
    ch_s = str(ch.base_version) if ch else str(chosen)
    mode = "next major" if majors_only else "next published release"
    subject = f"{package} " if package else ""
    if lat is not None and ch is not None and lat > ch:
        return (
            f"Bump {subject}{inst_s} → {ch_s} ({mode}; "
            f"latest on GitHub is {lat.base_version} — deferred)."
        )
    return f"Bump {subject}{inst_s} → {ch_s} ({mode} step)."
