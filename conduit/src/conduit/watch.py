"""Read-only Watch: pin at or past to_version plus leftover / surface binding gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

from conduit.detect.manifests import pin_for_packet_ecosystem, read_installed_by_ecosystem
from conduit.patcher.leftovers import Leftover, packet_has_call_site_rules, scan_packet_leftovers
from conduit.surface.evaluate import (
    binding_blocks_complete,
    binding_to_dict,
    evaluate_packet_binding,
)
from conduit.surface.types import BindingVerdict, VerdictStatus

WatchStatus = Literal[
    "pre_bump",
    "bump_dirty",
    "clean",
    "no_pin",
    "no_rules",
    "incomplete",
    "unverified",
]


@dataclass(frozen=True)
class WatchVerdict:
    """Outcome of a Watch predicate on one consumer tree."""

    status: WatchStatus
    pin: str | None
    from_version: str
    to_version: str
    leftovers: tuple[Leftover, ...]
    exit_code: int
    message: str
    binding: BindingVerdict | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "pin": self.pin,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "leftover_count": len(self.leftovers),
            "leftovers": [item.display() for item in self.leftovers],
            "exit_code": self.exit_code,
            "message": self.message,
        }
        if self.binding is not None:
            payload["completeness"] = binding_to_dict(self.binding)
        return payload


def _norm_version(value: str | None) -> str:
    return str(value or "").strip().lstrip("=vV")


def versions_match(a: str | None, b: str | None) -> bool:
    left = _norm_version(a)
    right = _norm_version(b)
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        return Version(left) == Version(right)
    except InvalidVersion:
        return False


def version_at_or_past(pin: str | None, target: str | None) -> bool:
    """True when pin is equal to or newer than target."""
    left = _norm_version(pin)
    right = _norm_version(target)
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        return Version(left) >= Version(right)
    except InvalidVersion:
        return False


def read_package_pin(root: Path, packet: dict) -> str | None:
    pkg = str(packet.get("package") or "")
    eco = str(packet.get("ecosystem") or "")
    by_eco = read_installed_by_ecosystem(root)
    return pin_for_packet_ecosystem(by_eco, pkg, eco or None)


def evaluate_watch(*, root: Path, packet: dict) -> WatchVerdict:
    """Gate leftovers + surface binding when pin is at or past to_version.

    Exit policy:
    - ``bump_dirty`` (exit 1): rules-bearing packet, pin >= to_version, leftovers remain.
    - ``incomplete`` (exit 1): pin >= to_version, proof-eligible binder unresolved.
    - ``unverified`` (exit 1): pin >= to_version, lexical binder still sees old sites
      the leftover scanner missed.
    - ``clean`` (exit 0): rules-bearing packet, pin >= to_version, no leftovers and
      binder does not block (completeness may still be ``unverified`` with zero obs).
    - ``no_rules`` (exit 0): pin-only packet at or past to_version; not ``clean``.
    - ``pre_bump`` (exit 0): pin still below to_version (warns when leftovers exist).
    - ``no_pin`` (exit 2): package pin missing from the consumer tree.
    """
    from_v = _norm_version(str(packet.get("from_version") or ""))
    to_v = _norm_version(str(packet.get("to_version") or ""))
    pin = read_package_pin(root, packet)
    has_rules = packet_has_call_site_rules(packet)
    leftovers = tuple(scan_packet_leftovers(root, packet)) if has_rules else ()
    binding = evaluate_packet_binding(root, packet) if has_rules else None

    if not pin:
        return WatchVerdict(
            status="no_pin",
            pin=None,
            from_version=from_v,
            to_version=to_v,
            leftovers=leftovers,
            exit_code=2,
            message=(
                f"no pin for package {packet.get('package')!r} "
                f"in ecosystem {packet.get('ecosystem')!r}"
            ),
            binding=binding,
        )

    at_or_past = version_at_or_past(pin, to_v)
    at_from = versions_match(pin, from_v)

    if at_or_past and not has_rules:
        return WatchVerdict(
            status="no_rules",
            pin=pin,
            from_version=from_v,
            to_version=to_v,
            leftovers=(),
            exit_code=0,
            message=(
                f"pin {pin} at or past to_version {to_v}; no call-site rules"
            ),
            binding=binding,
        )

    if at_or_past and leftovers:
        return WatchVerdict(
            status="bump_dirty",
            pin=pin,
            from_version=from_v,
            to_version=to_v,
            leftovers=leftovers,
            exit_code=1,
            message=(
                f"pin {pin} at or past to_version {to_v} with "
                f"{len(leftovers)} leftover call(s)"
            ),
            binding=binding,
        )

    if at_or_past and binding is not None and binding_blocks_complete(binding):
        status: WatchStatus = (
            "incomplete"
            if binding.status == VerdictStatus.INCOMPLETE
            else "unverified"
        )
        return WatchVerdict(
            status=status,
            pin=pin,
            from_version=from_v,
            to_version=to_v,
            leftovers=(),
            exit_code=1,
            message=(
                f"pin {pin} at or past to_version {to_v}; completeness "
                f"{binding.status.value}: {binding.explain()}"
            ),
            binding=binding,
        )

    if at_or_past and not leftovers:
        return WatchVerdict(
            status="clean",
            pin=pin,
            from_version=from_v,
            to_version=to_v,
            leftovers=(),
            exit_code=0,
            message=(
                f"pin {pin} at or past to_version {to_v}; no leftover packet calls"
            ),
            binding=binding,
        )

    if leftovers:
        label = "from_version" if at_from else "pre-bump"
        return WatchVerdict(
            status="pre_bump",
            pin=pin,
            from_version=from_v,
            to_version=to_v,
            leftovers=leftovers,
            exit_code=0,
            message=(
                f"warn: pin {pin} still at {label} "
                f"({from_v}); {len(leftovers)} leftover call(s) allowed"
            ),
            binding=binding,
        )

    return WatchVerdict(
        status="pre_bump",
        pin=pin,
        from_version=from_v,
        to_version=to_v,
        leftovers=(),
        exit_code=0,
        message=f"pin {pin} not at to_version {to_v}; tree clean of packet leftovers",
        binding=binding,
    )
