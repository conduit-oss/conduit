"""Shared outside-in records. Language passes all produce these."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entry:
    service: str
    package: str
    kind: str
    rel: str
    lineno: int
    symbol: str
    confidence: str
    detail: str = ""


@dataclass(frozen=True)
class Hop:
    rel: str
    lineno: int
    symbol: str
    expr: str


@dataclass(frozen=True)
class Chain:
    service: str
    package: str
    call_rel: str
    call_lineno: int
    call_symbol: str
    call_expr: str
    hops: tuple[Hop, ...]
    sdk_method: str
    kind: str
    params: tuple[str, ...]
    site_keywords: tuple[str, ...]
    response_fields: tuple[str, ...]
    confidence: str
    via_wrapper: bool
    #: Where a splatted / forwarded keyword was introduced: ``(name, rel, line, expr)``.
    param_origins: tuple[tuple[str, str, int, str], ...] = ()

    @property
    def call_site(self) -> str:
        return f"{self.call_rel}:{self.call_lineno}"


@dataclass(frozen=True)
class Break:
    kind: str
    rule_type: str
    old: str
    new: str
    detail: str
    confidence: str


@dataclass(frozen=True)
class Affected:
    chain: Chain
    breaks: tuple[Break, ...]


@dataclass(frozen=True)
class BlastReport:
    packet_id: str
    package: str
    from_version: str
    to_version: str
    affected: tuple[Affected, ...]


@dataclass
class ServiceMap:
    root: str
    entries: tuple[Entry, ...] = ()
    chains: tuple[Chain, ...] = ()
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def services(self) -> list[str]:
        found: list[str] = []
        for entry in self.entries:
            if entry.service not in found:
                found.append(entry.service)
        for chain in self.chains:
            if chain.service not in found:
                found.append(chain.service)
        return found
