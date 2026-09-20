"""Bind surface contracts to a consumer index and evaluate completeness."""

from __future__ import annotations

from conduit.surface.types import (
    BindingVerdict,
    Confidence,
    ConsumerIndex,
    FileIndex,
    LexicalOnlyContract,
    MatchEvidence,
    Observation,
    PacketContract,
    Spelling,
    SurfaceContract,
    UseKind,
    UseSite,
    VerdictStatus,
)

_PROOF_SPELLINGS = frozenset({Spelling.IMPORTED, Spelling.RECEIVER_MEMBER})


def bind(
    contracts: tuple[PacketContract, ...] | list[PacketContract],
    index: ConsumerIndex,
    package: str,
) -> tuple[Observation, ...]:
    """Match contracts against indexed uses; PR1 dispositions are still_old."""
    pkg = (package or "").strip()
    observations: list[Observation] = []
    for contract in contracts:
        for file_index in index.files:
            observations.extend(_bind_file(contract, file_index, pkg))
    return tuple(observations)


def evaluate_binding(
    contracts: tuple[PacketContract, ...] | list[PacketContract],
    observations: tuple[Observation, ...] | list[Observation],
) -> BindingVerdict:
    """Complete only from proof-eligible contracts with no unresolved still_old."""
    obs = tuple(observations)
    proof = [c for c in contracts if _is_proof_eligible(c)]
    if not proof:
        return BindingVerdict(
            status=VerdictStatus.UNVERIFIED,
            observations=obs,
            reasons=("no proof-eligible surface contracts",),
        )

    proof_ids = {c.surface_id for c in proof if _claims_proof_spellings(c)}
    if not proof_ids:
        # Proof-eligible but no imported/receiver_member claims → still unverified
        # until a richer contract declares proof spellings.
        return BindingVerdict(
            status=VerdictStatus.UNVERIFIED,
            observations=obs,
            reasons=("proof-eligible contracts claim no imported/receiver_member spellings",),
        )

    unresolved = [
        o
        for o in obs
        if o.surface_id in proof_ids and o.disposition == "still_old"
    ]
    if unresolved:
        return BindingVerdict(
            status=VerdictStatus.INCOMPLETE,
            observations=obs,
            reasons=(
                f"{len(unresolved)} unresolved observation(s) on proof-eligible surfaces",
            ),
        )
    return BindingVerdict(
        status=VerdictStatus.COMPLETE,
        observations=obs,
        reasons=(),
    )


def _is_proof_eligible(contract: PacketContract) -> bool:
    return bool(getattr(contract, "proof_eligible", False))


def _claims_proof_spellings(contract: PacketContract) -> bool:
    if not isinstance(contract, SurfaceContract):
        return False
    return bool(contract.spellings & _PROOF_SPELLINGS)


def _bind_file(
    contract: PacketContract,
    file_index: FileIndex,
    package: str,
) -> list[Observation]:
    export_path = contract.export_path
    if not export_path:
        return []
    member = export_path[-1]
    qualified = ".".join(export_path)
    old_callee = (
        contract.old_callee
        if isinstance(contract, LexicalOnlyContract)
        else qualified
    )
    use_kinds = (
        contract.use_kinds
        if isinstance(contract, SurfaceContract)
        else frozenset({UseKind.CALL, UseKind.DECORATOR})
    )
    spellings = (
        contract.spellings
        if isinstance(contract, SurfaceContract)
        else frozenset(
            {Spelling.QUALIFIED, Spelling.IMPORTED, Spelling.RECEIVER_MEMBER}
        )
    )
    imports_pkg = file_index.imports_package(package)
    out: list[Observation] = []
    for use in file_index.uses:
        if use.use_kind not in use_kinds:
            continue
        hit = _match_use(
            use=use,
            file_index=file_index,
            package=package,
            imports_pkg=imports_pkg,
            member=member,
            qualified=qualified,
            old_callee=old_callee,
            export_path=export_path,
            spellings=spellings,
        )
        if hit is None:
            continue
        confidence, evidence, spelling = hit
        out.append(
            Observation(
                surface_id=contract.surface_id,
                span=use.span,
                chain=use.chain,
                confidence=confidence,
                evidence=evidence,
                spelling=spelling,
            )
        )
    return out


def _match_use(
    *,
    use: UseSite,
    file_index: FileIndex,
    package: str,
    imports_pkg: bool,
    member: str,
    qualified: str,
    old_callee: str,
    export_path: tuple[str, ...],
    spellings: frozenset[Spelling],
) -> tuple[Confidence, MatchEvidence, Spelling] | None:
    chain = use.chain

    # Imported bare names before exact: `@validator` is IMPORT_ALIAS, not QUALIFIED.
    if Spelling.IMPORTED in spellings:
        imported = _imported_match(
            chain, file_index, package, member, qualified, old_callee
        )
        if imported:
            return Confidence.DEFINITE, MatchEvidence.IMPORT_ALIAS, Spelling.IMPORTED

    if Spelling.QUALIFIED in spellings:
        if chain == old_callee or chain == qualified:
            return Confidence.DEFINITE, MatchEvidence.EXACT, Spelling.QUALIFIED
        if package and (
            chain == f"{package}.{qualified}" or chain == f"{package}.{old_callee}"
        ):
            return Confidence.DEFINITE, MatchEvidence.EXACT, Spelling.QUALIFIED
        if imports_pkg and _package_rooted_suffix(
            chain, file_index, package, qualified, member
        ):
            return Confidence.DEFINITE, MatchEvidence.PACKAGE_ROOTED, Spelling.QUALIFIED

    if Spelling.RECEIVER_MEMBER in spellings and chain.endswith("." + member):
        if not imports_pkg:
            return None
        if _receiver_proven(chain, file_index, package, export_path):
            return (
                Confidence.DEFINITE,
                MatchEvidence.RECEIVER_TYPED,
                Spelling.RECEIVER_MEMBER,
            )
        return Confidence.POSSIBLE, MatchEvidence.MEMBER_ONLY, Spelling.RECEIVER_MEMBER

    return None


def _package_rooted_suffix(
    chain: str,
    file_index: FileIndex,
    package: str,
    qualified: str,
    member: str,
) -> bool:
    root = chain.split(".", 1)[0]
    origin = file_index.aliases.get(root)
    if origin is None:
        return False
    if origin == package or origin.startswith(package + "."):
        return chain.endswith("." + qualified) or chain.endswith("." + member)
    return False


def _imported_match(
    chain: str,
    file_index: FileIndex,
    package: str,
    member: str,
    qualified: str,
    old_callee: str,
) -> bool:
    root = chain.split(".", 1)[0]
    origin = file_index.aliases.get(root)
    if origin is None:
        return False
    if not (origin == package or origin.startswith(package + ".")):
        return False
    leaf = origin.rsplit(".", 1)[-1]
    old_leaf = old_callee.split(".")[-1]
    owner = qualified.split(".")[0] if qualified else ""
    # Bare imported name used as call/decorator: validator after `from pkg import validator`
    if chain == root:
        return leaf in {member, old_leaf} or origin in {
            qualified,
            old_callee,
            f"{package}.{qualified}" if package else "",
            f"{package}.{old_callee}" if package else "",
        }
    # Imported owner + member: BaseModel.dict after `from pydantic import BaseModel`
    if chain.endswith("." + member):
        return leaf == owner or origin.endswith("." + owner)
    return False


def _receiver_proven(
    chain: str,
    file_index: FileIndex,
    package: str,
    export_path: tuple[str, ...],
) -> bool:
    receiver = chain.rsplit(".", 1)[0]
    owner = export_path[0] if export_path else ""
    candidates = {
        owner,
        ".".join(export_path[:-1]) if len(export_path) > 1 else owner,
        f"{package}.{owner}" if package and owner else "",
    }
    candidates.discard("")

    type_name = file_index.annotations.get(receiver) or file_index.constructors.get(
        receiver
    )
    if type_name and _type_matches(type_name, file_index, candidates, package, owner):
        return True
    # self / cls on a local subclass of the exported owner
    if receiver in {"self", "cls"}:
        for bases in file_index.local_bases.values():
            if any(_base_matches(base, candidates, package, owner) for base in bases):
                return True
    return False


def _type_matches(
    type_name: str,
    file_index: FileIndex,
    candidates: set[str],
    package: str,
    owner: str,
) -> bool:
    if type_name in candidates:
        return True
    root = type_name.split(".", 1)[0]
    if root in file_index.aliases:
        origin = file_index.aliases[root]
        rest = type_name[len(root) :]
        resolved = origin + rest
        if resolved in candidates or any(
            resolved.endswith("." + c) or c.endswith(resolved) for c in candidates
        ):
            return True
    if root in file_index.local_bases:
        return any(
            _base_matches(base, candidates, package, owner)
            for base in file_index.local_bases[root]
        )
    return _base_matches(type_name, candidates, package, owner)


def _base_matches(
    base: str,
    candidates: set[str],
    package: str,
    owner: str,
) -> bool:
    if base in candidates:
        return True
    if owner and (base == owner or base.endswith("." + owner)):
        return True
    if package and owner and base == f"{package}.{owner}":
        return True
    return False
