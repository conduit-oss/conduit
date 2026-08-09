"""Detect module plugin interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conduit.detect.models import ChangeSignal

if TYPE_CHECKING:
    from conduit.detect.client_state import PackageClientState


@dataclass
class DetectContext:
    """Context passed to vendor detect modules."""

    repo_root: Path
    installed: dict[str, str] = field(default_factory=dict)  # package -> version
    package_states: dict[str, "PackageClientState"] = field(default_factory=dict)
    demo: bool = False  # use offline fixtures; live sources otherwise
    verbose: bool = False
    majors_only: bool = True  # one major version step at a time
    extra: dict[str, Any] = field(default_factory=dict)


class DetectModule(ABC):
    """Vendor-specific signal source plugged into core detect.

    Successor / version policy (shared across modules):
    - Models/endpoints: prefer intermediate documented steps
      (``conduit.detect.successor_policy``).
    - Package/SDK versions: prefer the next major (or next release) rather than
      jumping to absolute latest (``conduit.detect.version_steps``).
    """

    name: str = "module"
    packages: list[str] = []

    def applies(self, installed: dict[str, str]) -> bool:
        if not self.packages:
            return True
        wanted = {p.lower() for p in self.packages}
        return any(name.lower() in wanted for name in installed)

    def evidence_seeds(self) -> list[str]:
        """Canonical doc URLs for LLM packet synthesis (override per vendor)."""
        return []

    def evidence_hosts(self) -> list[str]:
        """Host allowlist for fetch/search evidence (override per vendor)."""
        return []

    def evidence_queries(self, *, from_version: str, to_version: str) -> list[str]:
        """Web search queries for LLM evidence (override per vendor)."""
        return []

    @abstractmethod
    def run(self, ctx: DetectContext) -> list[ChangeSignal]:
        raise NotImplementedError
