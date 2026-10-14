"""Feed the outside-in graph into Watch, apply, and the map command."""

from __future__ import annotations

from pathlib import Path

from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.blast import blast_radius
from conduit.outside_in.catalog import catalog_spec
from conduit.outside_in.model import BlastReport, Chain, ServiceMap
from conduit.patcher.leftovers import Leftover


def build_service_map(
    root: Path,
    *,
    service: str | None = None,
    packet: dict | None = None,
) -> tuple[ServiceMap, BlastReport | None]:
    extra: tuple[str, ...] = ()
    if packet is not None:
        package = str(packet.get("package") or "").strip()
        if package:
            extra = (package,)
    service_map = analyze_repo(root, service=service, extra_modules=extra)
    blast = blast_radius(service_map, packet) if packet is not None else None
    return service_map, blast


def merge_wrapped_leftovers(
    root: Path,
    packet: dict,
    leftovers: list[Leftover],
) -> list[Leftover]:
    """Add call sites that reach a packet old_callee through user wrappers.

    Direct SDK lines already found by the import scanner stay as they are.
    HTTP calls are reported by ``conduit map`` and are not leftovers: apply
    cannot rewrite a raw REST body from an SDK callee rule.
    """
    package = str(packet.get("package") or "").strip()
    if catalog_spec(package) is None:
        return leftovers
    service_map = analyze_repo(root, service=package)
    existing = {(item.rel, item.lineno) for item in leftovers}
    out = list(leftovers)
    for chain in service_map.chains:
        if chain.kind not in {"sdk_call", "db_call", "cloud_call"}:
            continue
        matched = _matching_old_callee(chain, packet)
        if matched is None:
            continue
        key = (chain.call_rel, chain.call_lineno)
        if key in existing:
            continue
        out.append(
            Leftover(
                rel=chain.call_rel,
                callee=matched,
                reason=_leftover_reason(chain),
                lineno=chain.call_lineno,
            )
        )
        existing.add(key)
    return out


def direct_entry_scope(
    root: Path, packages: list[str]
) -> tuple[list[Path], set[str]]:
    """Files with a resolved SDK or HTTP call the import prune can miss.

    The returned relative paths are safe to treat as vendor context: the
    graph already tied a call in that file to the package. Upstream wrapper
    callers are not included, so a file that only calls ``complete()`` is
    not opened up to suffix-based parameter renames.
    """
    from conduit.outside_in.graph import analyze_python

    specs = tuple(spec for spec in (catalog_spec(name) for name in packages) if spec is not None)
    if not specs:
        return [], set()
    service_map = analyze_python(root, specs)
    rels: set[str] = set()
    for chain in service_map.chains:
        if chain.via_wrapper:
            continue
        if chain.kind not in {
            "sdk_call",
            "sdk_constructor",
            "db_call",
            "db_constructor",
            "cloud_call",
            "cloud_constructor",
            "http_call",
            "webhook",
        }:
            continue
        rels.add(chain.call_rel)
    paths: list[Path] = []
    root = root.resolve()
    for rel in sorted(rels):
        path = root / rel
        if path.is_file():
            paths.append(path)
    return paths, rels


def merge_allowlist(files: list[Path], extra: list[Path]) -> list[Path]:
    seen = {path.resolve() for path in files}
    out = list(files)
    for path in extra:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        out.append(path)
        seen.add(resolved)
    return out


def _matching_old_callee(chain: Chain, packet: dict) -> str | None:
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        old = str(rule.get("old_callee") or "").strip()
        if not old:
            continue
        if chain.sdk_method == old or chain.sdk_method.endswith("." + old):
            return old
    return None


def _leftover_reason(chain: Chain) -> str:
    if chain.via_wrapper:
        path = " -> ".join(hop.expr for hop in chain.hops)
        return f"outside-in wrapper chain {path} [{chain.confidence}]"
    return f"outside-in call via alias or re-export [{chain.confidence}]"

