"""Bind a published catalog packet to a consumer's installed version."""

from __future__ import annotations

import copy
from typing import Any

SNAPSHOT_FLOORS = frozenset({"", "0", "0.0.0"})


def is_snapshot_floor(value: str | None) -> bool:
    return str(value or "").strip() in SNAPSHOT_FLOORS


def bind_packet_to_client(
    packet: dict[str, Any],
    *,
    installed_version: str | None,
) -> dict[str, Any]:
    """Copy client ``installed_version`` onto floor ``from_version`` fields.

    Catalog snapshots use ``from_version`` ``0`` as a floor, not a PyPI release.
    Apply/export-delta/oracle should use the consumer pin for the packet
    ecosystem instead. The published JSON on disk is not rewritten.
    """
    installed = str(installed_version or "").strip()
    if not installed or is_snapshot_floor(installed):
        return packet

    bound = copy.deepcopy(packet)
    if is_snapshot_floor(str(bound.get("from_version") or "")):
        bound["from_version"] = installed
    rules: list[dict[str, Any]] = []
    for rule in bound.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        item = dict(rule)
        if str(item.get("type") or "") == "DEPENDENCY_BUMP" and is_snapshot_floor(
            str(item.get("from_version") or "")
        ):
            item["from_version"] = installed
        rules.append(item)
    if rules or bound.get("rules"):
        bound["rules"] = rules
    return bound
