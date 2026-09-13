"""Publisher snapshot packets from detect (no consumer repo)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from conduit.detect.models import ChangeSignal
from conduit.detect.modules.discovery import load_modules
from conduit.detect.modules.openai.workers.sdk_release import packet_ecosystem_for
from conduit.detect.orchestrator import run_detect
from conduit.packet.cache import save_packet
from conduit.packet.synthesize import packet_from_signals, synthesize_from_evidence
from conduit.packet.validate import validate_packet

SNAPSHOT_FLOOR = "0"
_SDK_BUMP_TYPES = frozenset({"SDK_MAJOR_BUMP", "SDK_BUMP", "DEPENDENCY_BUMP"})
_PACKET_ECOSYSTEMS = frozenset({"pypi", "npm", "go", "maven", "other"})


def snapshot_packet_id(package: str, ecosystem: str, to_version: str) -> str:
    return f"{package}-{ecosystem}-{to_version}"


def snapshot_filename(package: str, ecosystem: str, to_version: str) -> str:
    safe = lambda s: str(s).replace("/", "_").replace("\\", "_").replace(" ", "")
    return f"{safe(package)}-{safe(ecosystem)}-{safe(to_version)}.json"


def dep_ecosystems_for_packet(packet_eco: str) -> list[str]:
    if packet_eco == "npm":
        return ["npm"]
    if packet_eco == "go":
        return ["go"]
    if packet_eco == "maven":
        return ["maven", "gradle"]
    return ["pip", "pyproject"]


def _parse_ver(value: str | None) -> Version | None:
    if not value:
        return None
    try:
        return Version(str(value).lstrip("v"))
    except InvalidVersion:
        return None


def _rule_key(rule: dict[str, Any]) -> str:
    return json.dumps(rule, sort_keys=True)


def _is_bump(rule: dict[str, Any]) -> bool:
    return str(rule.get("type") or "") == "DEPENDENCY_BUMP"


@dataclass
class SnapshotTarget:
    package: str
    ecosystem: str
    to_version: str


@dataclass
class SnapshotWrite:
    path: Path
    packet: dict[str, Any]
    skipped: bool = False
    skip_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def snapshot_targets_from_signals(
    signals: list[ChangeSignal],
    *,
    package: str | None = None,
) -> list[SnapshotTarget]:
    """Unique (package, ecosystem, to_version) from SDK bump signals."""
    found: dict[tuple[str, str], SnapshotTarget] = {}
    want = (package or "").lower()

    def _add(pkg: str, eco: str, to_v: str) -> None:
        pkg = str(pkg or "").strip()
        eco = str(eco or "").strip().lower()
        to_v = str(to_v or "").strip()
        if not pkg or not eco or not to_v:
            return
        if eco not in _PACKET_ECOSYSTEMS:
            return
        if want and pkg.lower() != want:
            return
        key = (pkg.lower(), eco)
        prev = found.get(key)
        new_ver = _parse_ver(to_v)
        old_ver = _parse_ver(prev.to_version) if prev else None
        if prev is None or (new_ver is not None and (old_ver is None or new_ver > old_ver)):
            found[key] = SnapshotTarget(package=pkg, ecosystem=eco, to_version=to_v)

    for signal in signals:
        pkg = signal.package
        if signal.change_type in {"SDK_MAJOR_BUMP", "SDK_BUMP"} and signal.to_version:
            eco = signal.ecosystem
            if not eco:
                for rule in signal.suggested_rules:
                    if _is_bump(rule):
                        eco = packet_ecosystem_for(rule.get("ecosystems") or [])
                        pkg = str(rule.get("package") or pkg)
                        break
            if eco:
                _add(pkg, eco, str(signal.to_version))
        for rule in signal.suggested_rules:
            if not _is_bump(rule):
                continue
            to_v = rule.get("to_version")
            eco = packet_ecosystem_for(rule.get("ecosystems") or [])
            rpkg = str(rule.get("package") or pkg)
            if to_v and eco:
                _add(rpkg, eco, str(to_v))
    return list(found.values())


def find_snapshot_at(
    out_dir: Path,
    *,
    package: str,
    ecosystem: str,
    to_version: str,
) -> dict[str, Any] | None:
    """Return an existing snapshot whose to_version equals ``to_version``."""
    want = _parse_ver(to_version)
    if want is None or not out_dir.is_dir():
        return None
    for path in out_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("package") or "").lower() != package.lower():
            continue
        if str(data.get("ecosystem") or "").lower() != ecosystem.lower():
            continue
        if _parse_ver(str(data.get("to_version") or "")) == want:
            return data
    return None


def find_previous_snapshot(
    out_dir: Path,
    *,
    package: str,
    ecosystem: str,
    to_version: str,
    previous_path: Path | None = None,
) -> dict[str, Any] | None:
    if previous_path is not None:
        if not previous_path.is_file():
            return None
        data = json.loads(previous_path.read_text(encoding="utf-8"))
        if str(data.get("package") or "").lower() != package.lower():
            return None
        if str(data.get("ecosystem") or "").lower() != ecosystem.lower():
            return None
        return data

    want_to = _parse_ver(to_version)
    if want_to is None:
        return None
    best: tuple[Version, dict[str, Any]] | None = None
    if not out_dir.is_dir():
        return None
    for path in out_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("package") or "").lower() != package.lower():
            continue
        if str(data.get("ecosystem") or "").lower() != ecosystem.lower():
            continue
        parsed = _parse_ver(str(data.get("to_version") or ""))
        if parsed is None or parsed >= want_to:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, data)
    return best[1] if best else None


def subtract_previous_rules(
    rules: list[dict[str, Any]],
    previous: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if previous is None:
        return list(rules)
    seen = {
        _rule_key(r)
        for r in (previous.get("rules") or [])
        if isinstance(r, dict) and not _is_bump(r)
    }
    out: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if _is_bump(rule):
            out.append(rule)
            continue
        if _rule_key(rule) in seen:
            continue
        out.append(rule)
    return out


def _ensure_hop_bump(
    rules: list[dict[str, Any]],
    *,
    package: str,
    ecosystem: str,
    from_version: str,
    to_version: str,
) -> list[dict[str, Any]]:
    dep_ecos = dep_ecosystems_for_packet(ecosystem)
    out: list[dict[str, Any]] = []
    bump: dict[str, Any] | None = None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "DEPENDENCY_BUMP":
            out.append(rule)
            continue
        pkg = str(rule.get("package") or package).lower()
        if pkg != package.lower():
            out.append(rule)
            continue
        bump = dict(rule)
    if from_version == to_version:
        return out
    if bump is None:
        bump = {
            "type": "DEPENDENCY_BUMP",
            "package": package,
            "from_version": from_version,
            "to_version": to_version,
            "ecosystems": dep_ecos,
            "reason": f"Catalog snapshot bump {package} {from_version} → {to_version}.",
        }
    else:
        bump["package"] = package
        bump["from_version"] = from_version
        bump["to_version"] = to_version
        bump["ecosystems"] = dep_ecos
    out.insert(0, bump)
    return out


def _signals_for_ecosystem(
    signals: list[ChangeSignal],
    *,
    package: str,
    ecosystem: str,
) -> list[ChangeSignal]:
    """Shared scrape plus SDK bumps that belong to this packet ecosystem."""
    out: list[ChangeSignal] = []
    for signal in signals:
        if signal.change_type in {"SDK_MAJOR_BUMP", "SDK_BUMP"}:
            eco = signal.ecosystem
            if not eco:
                for rule in signal.suggested_rules:
                    if _is_bump(rule):
                        eco = packet_ecosystem_for(rule.get("ecosystems") or [])
                        break
            if eco and eco.lower() != ecosystem.lower():
                continue
            if signal.package.lower() != package.lower():
                continue
        out.append(signal)
    return out


def build_snapshot_packet(
    signals: list[ChangeSignal],
    *,
    package: str,
    ecosystem: str,
    from_version: str,
    to_version: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scoped = _signals_for_ecosystem(signals, package=package, ecosystem=ecosystem)
    if package.lower() == "openai":
        from conduit.detect.modules.openai.sdk_callee_migration import (
            apply_sdk_callee_migration,
        )

        scoped, _ = apply_sdk_callee_migration(scoped, client_state=None)
    packet = packet_from_signals(
        scoped,
        package=package,
        ecosystem=ecosystem,
        from_version=from_version,
        to_version=to_version,
        companion_signals=scoped,
    )
    packet["packet_id"] = snapshot_packet_id(package, ecosystem, to_version)
    packet["from_version"] = from_version
    packet["to_version"] = to_version
    packet["ecosystem"] = ecosystem
    prev_label = previous.get("to_version") if previous else None
    packet["notes"] = (
        f"Catalog snapshot targeting {package} {to_version} ({ecosystem}). "
        + (
            f"Previous snapshot {prev_label}."
            if prev_label
            else "First snapshot (from_version 0 is a floor, not a PyPI/npm release)."
        )
    )
    packet["rules"] = _ensure_hop_bump(
        subtract_previous_rules(list(packet.get("rules") or []), previous),
        package=package,
        ecosystem=ecosystem,
        from_version=from_version,
        to_version=to_version,
    )
    return packet


def write_snapshots(
    signals: list[ChangeSignal],
    *,
    out_dir: Path,
    package: str | None = None,
    ecosystem: str | None = None,
    previous_path: Path | None = None,
    out_path: Path | None = None,
    enrich: bool = False,
    log=None,
) -> list[SnapshotWrite]:
    """Write one snapshot file per (package, ecosystem) target. Scrape once."""
    targets = snapshot_targets_from_signals(signals, package=package)
    if ecosystem:
        want = ecosystem.lower()
        targets = [t for t in targets if t.ecosystem.lower() == want]

    enriched_signals = list(signals)
    enrich_warnings: list[str] = []
    if enrich and targets:
        first = targets[0]
        base = packet_from_signals(
            signals,
            package=first.package,
            ecosystem=first.ecosystem,
            from_version=SNAPSHOT_FLOOR,
            to_version=first.to_version,
            companion_signals=signals,
        )
        base, enrich_warnings = synthesize_from_evidence(
            package=first.package,
            from_version=SNAPSHOT_FLOOR,
            to_version=first.to_version,
            ecosystem=first.ecosystem,
            signals=signals,
            base=base,
            root=None,
            source_packet=None,
            publisher=True,
            log=log,
        )
        extra_rules = [
            r
            for r in (base.get("rules") or [])
            if isinstance(r, dict) and not _is_bump(r)
        ]
        if extra_rules:
            enriched_signals = list(signals) + [
                ChangeSignal(
                    source="llm:enrich",
                    package=first.package,
                    change_type="PARAM_RENAME",
                    suggested_rules=extra_rules,
                )
            ]

    results: list[SnapshotWrite] = []
    for target in targets:
        existing = find_snapshot_at(
            out_dir,
            package=target.package,
            ecosystem=target.ecosystem,
            to_version=target.to_version,
        )
        if existing is not None:
            path = out_dir / snapshot_filename(
                target.package, target.ecosystem, target.to_version
            )
            results.append(
                SnapshotWrite(
                    path=path,
                    packet=existing,
                    skipped=True,
                    skip_reason=(
                        "already have "
                        + snapshot_packet_id(
                            target.package, target.ecosystem, target.to_version
                        )
                    ),
                    warnings=list(enrich_warnings),
                )
            )
            continue
        prev = find_previous_snapshot(
            out_dir,
            package=target.package,
            ecosystem=target.ecosystem,
            to_version=target.to_version,
            previous_path=previous_path if len(targets) == 1 else None,
        )
        from_v = str((prev or {}).get("to_version") or SNAPSHOT_FLOOR)
        packet = build_snapshot_packet(
            enriched_signals,
            package=target.package,
            ecosystem=target.ecosystem,
            from_version=from_v,
            to_version=target.to_version,
            previous=prev,
        )
        dest = out_path if out_path is not None and len(targets) == 1 else (
            out_dir / snapshot_filename(
                target.package, target.ecosystem, target.to_version
            )
        )
        save_packet(dest, packet)
        results.append(
            SnapshotWrite(
                path=dest,
                packet=packet,
                warnings=list(enrich_warnings),
            )
        )
    return results


def run_packet_from_detect(
    *,
    module: str,
    out_dir: Path,
    package: str | None = None,
    ecosystem: str | None = None,
    previous_path: Path | None = None,
    out_path: Path | None = None,
    demo: bool = False,
    enrich: bool = False,
    log=None,
) -> tuple[list[SnapshotWrite], list[str]]:
    modules = list(load_modules(names=[module]))
    if not modules:
        available = ", ".join(sorted(m.name for m in load_modules()) ) or "(none)"
        raise ValueError(f"Unknown detect module {module!r}. Available: {available}")
    mod = modules[0]
    pkg = package or ((mod.packages[0] if mod.packages else None) or mod.name)
    detected = run_detect(
        Path("."),
        skip_lockfile=True,
        scan_client=False,
        catalog_latest=True,
        module_names=[mod.name],
        demo=demo,
        log=log,
    )
    writes = write_snapshots(
        detected.signals,
        out_dir=out_dir,
        package=pkg,
        ecosystem=ecosystem,
        previous_path=previous_path,
        out_path=out_path,
        enrich=enrich,
        log=log,
    )
    warnings = list(detected.warnings)
    for item in writes:
        warnings.extend(item.warnings)
        for err in validate_packet(item.packet):
            warnings.append(f"{item.path.name}: {err}")
    return writes, warnings
