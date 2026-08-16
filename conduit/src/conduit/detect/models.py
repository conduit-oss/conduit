"""Unified change signals produced by lockfile diff and vendor modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ChangeSignal:
    """Normalized signal that feeds packet synthesis and apply."""

    source: str  # e.g. "lockfile", "module:openai"
    package: str
    change_type: str
    severity: str = "WARNING"
    from_version: str | None = None
    to_version: str | None = None
    ecosystem: str | None = None
    affected_pattern: str | None = None
    replacement_pattern: str | None = None
    description: str | None = None
    source_url: str | None = None
    deadline: str | None = None
    hints: dict[str, Any] = field(default_factory=dict)
    suggested_rules: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {k: v for k, v in data.items() if v is not None and v != {} and v != []}


@dataclass
class VersionJump:
    name: str
    from_version: str
    to_version: str
    ecosystem: str  # pypi | npm | go
    manifest: str
    kind: str = "bump"  # bump | add | remove
    scope: str | None = None

    @property
    def is_major(self) -> bool:
        def major(v: str) -> int:
            try:
                return int(v.lstrip("v^~>=< ").split(".")[0] or "0")
            except ValueError:
                return 0

        return major(self.from_version) != major(self.to_version)

    def _ecosystems(self) -> list[str]:
        if self.ecosystem == "pypi":
            return ["pip", "pyproject"]
        if self.ecosystem == "npm":
            return ["npm"]
        if self.ecosystem == "go":
            return ["go"]
        return []

    def to_signal(self) -> ChangeSignal:
        ecosystems = self._ecosystems()
        scope = self.scope or "main"
        if self.kind == "add":
            return ChangeSignal(
                source="lockfile",
                package=self.name,
                change_type="PACKAGE_ADDED",
                severity="WARNING",
                to_version=self.to_version or None,
                ecosystem=self.ecosystem,
                description=(
                    f"{self.name} added {self.to_version} ({self.manifest})"
                ),
                suggested_rules=[
                    {
                        "type": "DEPENDENCY_ADD",
                        "package": self.name,
                        "to_version": self.to_version,
                        "ecosystems": ecosystems,
                        "scope": scope,
                    }
                ],
            )
        if self.kind == "remove":
            return ChangeSignal(
                source="lockfile",
                package=self.name,
                change_type="PACKAGE_REMOVED",
                severity="WARNING",
                from_version=self.from_version or None,
                ecosystem=self.ecosystem,
                description=(
                    f"{self.name} removed {self.from_version} ({self.manifest})"
                ),
                suggested_rules=[
                    {
                        "type": "DEPENDENCY_REMOVE",
                        "package": self.name,
                        "from_version": self.from_version,
                        "ecosystems": ecosystems,
                        "scope": scope,
                    }
                ],
            )
        return ChangeSignal(
            source="lockfile",
            package=self.name,
            change_type="SDK_MAJOR_BUMP" if self.is_major else "DEPENDENCY_BUMP",
            severity="CRITICAL" if self.is_major else "INFO",
            from_version=self.from_version,
            to_version=self.to_version,
            ecosystem=self.ecosystem,
            description=(
                f"{self.name} {self.from_version} -> {self.to_version} "
                f"({self.manifest})"
            ),
            suggested_rules=[
                {
                    "type": "DEPENDENCY_BUMP",
                    "package": self.name,
                    "from_version": self.from_version,
                    "to_version": self.to_version,
                    "ecosystems": ecosystems,
                }
            ],
        )
