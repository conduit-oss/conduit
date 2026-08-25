"""Detect official SDK usage in migrated files (package-agnostic)."""

from __future__ import annotations

import re
from typing import Any

from conduit.anticheat.rules import imports_package, packet_new_callees, packet_package
from conduit.anticheat.vendor import legal_surface_hints


def _title_case_package(package: str) -> str:
    parts = re.split(r"[-_.]", package.strip())
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def legal_surface_patterns(packet: dict[str, Any]) -> list[str]:
    """Patterns that indicate honest official-SDK migration in a file."""
    pkg = packet_package(packet)
    patterns: list[str] = []
    hints = legal_surface_hints(packet)

    if pkg:
        patterns.append(f"from {pkg} import")
        patterns.append(f"import {pkg}")
        title = _title_case_package(pkg)
        patterns.append(f"{title}(")

    if hints.get("modern_callees_from_packet", True):
        for callee in packet_new_callees(packet):
            if callee:
                patterns.append(callee)
                root = callee.split(".", 1)[0]
                if root:
                    patterns.append(f"{root}.")

    for item in hints.get("client_constructors") or []:
        text = str(item)
        if "{package}" in text:
            text = text.replace("{package}", pkg)
        if "{PackageTitleCase}" in text:
            text = text.replace("{PackageTitleCase}", _title_case_package(pkg))
        if text:
            patterns.append(text)

    for item in hints.get("callee_suffixes") or []:
        if item:
            patterns.append(str(item))

    for item in hints.get("official_imports") or []:
        text = str(item)
        if "{package}" in text:
            text = text.replace("{package}", pkg)
        if text:
            patterns.append(text)

    # dedupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def matches_legal_surface(text: str, packet: dict[str, Any]) -> bool:
    """True when file shows official SDK import or packet new callees."""
    if not text:
        return False
    pkg = packet_package(packet)
    if pkg and imports_package(text, pkg):
        for pattern in legal_surface_patterns(packet):
            if pattern in text:
                return True
        # Import alone with any dotted package use is usually honest.
        if re.search(rf"\b{re.escape(pkg)}\.", text):
            return True
    for pattern in legal_surface_patterns(packet):
        if pattern and pattern in text:
            return True
    return False


COMPAT_WRAPPER_KINDS = frozenset(
    {
        "compat_wrapper",
        "compat",
        "fake_client",
        "fake_sdk",
        "compatwrapper",
    }
)
