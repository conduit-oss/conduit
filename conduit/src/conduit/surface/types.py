"""Domain types for evidence-bound surface binding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Literal


class Confidence(str, Enum):
    DEFINITE = "definite"
    POSSIBLE = "possible"


class MatchEvidence(IntEnum):
    """Ordered ladder (C1 graft). Stronger evidence compares with ``>``."""

    MEMBER_ONLY = 1
    RECEIVER_TYPED = 2
    PACKAGE_ROOTED = 3
    IMPORT_ALIAS = 4
    EXACT = 5


class Spelling(str, Enum):
    QUALIFIED = "qualified"
    IMPORTED = "imported"
    RECEIVER_MEMBER = "receiver_member"


class UseKind(str, Enum):
    CALL = "call"
    DECORATOR = "decorator"


class VerdictStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class RenameTerminal:
    """Rename only the leaf of an observed chain; always preserves the receiver prefix."""

    old_member: str
    new_member: str


@dataclass(frozen=True)
class ReplaceResolvedExport:
    """Replace a proven qualified/imported export chain with another export chain."""

    source: tuple[str, ...]
    target: tuple[str, ...]


RewriteIntent = RenameTerminal | ReplaceResolvedExport


@dataclass(frozen=True)
class Rewrite:
    """Deprecated wire-compile shape; prefer ``RewriteIntent`` via ``intent_from``."""

    export_path: tuple[str, ...] | None = None
    member: str | None = None


@dataclass(frozen=True)
class SurfaceContract:
    """Proof-eligible contract with declared consumer spellings."""

    surface_id: str
    export_path: tuple[str, ...]
    use_kinds: frozenset[UseKind]
    spellings: frozenset[Spelling]
    rewrite: RewriteIntent | Rewrite
    target_files: tuple[str, ...] = ("*",)
    proof_eligible: bool = True


@dataclass(frozen=True)
class LexicalOnlyContract:
    """Legacy old_callee rule: runnable later, never proof-eligible alone."""

    surface_id: str
    old_callee: str
    new_callee: str
    export_path: tuple[str, ...]
    target_files: tuple[str, ...] = ("*",)
    proof_eligible: bool = False


PacketContract = SurfaceContract | LexicalOnlyContract


@dataclass(frozen=True)
class SourceSpan:
    path: str
    line: int
    col: int = 0


@dataclass(frozen=True)
class UseSite:
    span: SourceSpan
    use_kind: UseKind
    chain: str
    enclosing_class: str | None = None


@dataclass(frozen=True)
class FileIndex:
    """Per-file import graph and use sites; package-agnostic."""

    rel: str
    aliases: dict[str, str]
    local_bases: dict[str, tuple[str, ...]]
    annotations: dict[str, str]
    constructors: dict[str, str]
    uses: tuple[UseSite, ...]

    def imports_package(self, package: str) -> bool:
        pkg = (package or "").strip()
        if not pkg:
            return False
        for origin in self.aliases.values():
            if origin == pkg or origin.startswith(pkg + "."):
                return True
        return False


@dataclass(frozen=True)
class ConsumerIndex:
    files: tuple[FileIndex, ...]


@dataclass(frozen=True)
class Observation:
    surface_id: str
    span: SourceSpan
    chain: str
    confidence: Confidence
    evidence: MatchEvidence
    spelling: Spelling
    disposition: Literal["still_old"] = "still_old"
    enclosing_class: str | None = None
    resolved_export: tuple[str, ...] = ()


@dataclass(frozen=True)
class BindingVerdict:
    status: VerdictStatus
    observations: tuple[Observation, ...]
    reasons: tuple[str, ...] = ()

    def explain(self) -> str:
        if self.reasons:
            return "; ".join(self.reasons)
        return f"status={self.status.value} observations={len(self.observations)}"
