"""Read-only Watch: pin reached to_version plus leftover old_callee calls."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

from conduit.detect.manifests import pin_for_packet_ecosystem, read_installed_by_ecosystem
from conduit.patcher.leftovers import Leftover, scan_packet_leftovers

WatchStatus = Literal["pre_bump", "bump_dirty", "clean", "no_pin"]


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pin": self.pin,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "leftover_count": len(self.leftovers),
            "leftovers": [item.display() for item in self.leftovers],
            "exit_code": self.exit_code,
            "message": self.message,
        }


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


def read_package_pin(root: Path, packet: dict) -> str | None:
    pkg = str(packet.get("package") or "")
    eco = str(packet.get("ecosystem") or "")
    by_eco = read_installed_by_ecosystem(root)
    return pin_for_packet_ecosystem(by_eco, pkg, eco or None)


def evaluate_watch(*, root: Path, packet: dict) -> WatchVerdict:
    """Gate: fail only when pin is at to_version and leftovers remain."""
    from_v = _norm_version(str(packet.get("from_version") or ""))
    to_v = _norm_version(str(packet.get("to_version") or ""))
    pin = read_package_pin(root, packet)
    leftovers = tuple(scan_packet_leftovers(root, packet))

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
        )

    at_to = versions_match(pin, to_v)
    at_from = versions_match(pin, from_v)

    if at_to and leftovers:
        return WatchVerdict(
            status="bump_dirty",
            pin=pin,
            from_version=from_v,
            to_version=to_v,
            leftovers=leftovers,
            exit_code=1,
            message=(
                f"pin {pin} reached to_version {to_v} with "
                f"{len(leftovers)} leftover call(s)"
            ),
        )

    if at_to and not leftovers:
        return WatchVerdict(
            status="clean",
            pin=pin,
            from_version=from_v,
            to_version=to_v,
            leftovers=(),
            exit_code=0,
            message=f"pin {pin} at to_version {to_v}; no leftover packet calls",
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
        )

    return WatchVerdict(
        status="pre_bump",
        pin=pin,
        from_version=from_v,
        to_version=to_v,
        leftovers=(),
        exit_code=0,
        message=f"pin {pin} not at to_version {to_v}; tree clean of packet leftovers",
    )
