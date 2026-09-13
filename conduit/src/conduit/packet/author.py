"""Guided packet authoring helpers (conduit packet new / diff / test)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from conduit.detect.manifests import (
    pin_for_packet_ecosystem,
    read_installed_by_ecosystem,
)
from conduit.detect.modules.discovery import load_modules
from conduit.packet.from_detect import find_previous_snapshot, run_packet_from_detect
from conduit.packet.validate import validate_packet
from conduit.scaffold.packet_init import scaffold_packet


def _safe_slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-._")
    return text or "packet"


def _rule_key(rule: dict[str, Any]) -> str:
    from conduit.packet.from_detect import _rule_key as _key

    return _key(rule)


def default_packet_out_path(
    *,
    package: str,
    ecosystem: str,
    to_version: str,
    out_dir: Path | None = None,
) -> Path:
    base = out_dir or Path("packets")
    name = f"{_safe_slug(package)}-{_safe_slug(ecosystem)}-{_safe_slug(to_version)}.json"
    return base / name


def resolve_module_name(package: str) -> str | None:
    want = (package or "").strip().lower()
    if not want:
        return None
    for mod in load_modules():
        if mod.name.lower() == want:
            return mod.name
        pkg = str(getattr(mod, "package", "") or "").lower()
        if pkg and pkg == want:
            return mod.name
    return None


def consumer_pin(root: Path, package: str, ecosystem: str) -> str | None:
    by_eco = read_installed_by_ecosystem(root)
    return pin_for_packet_ecosystem(by_eco, package, ecosystem) or None


def summarize_rule(rule: dict[str, Any]) -> str:
    rtype = str(rule.get("type") or "?")
    if rtype == "DEPENDENCY_BUMP":
        return (
            f"{rtype} {rule.get('package')} "
            f"{rule.get('from_version')} -> {rule.get('to_version')}"
        )
    if rtype == "EXACT_STRING_REPLACE":
        return f"{rtype} {rule.get('old')!r} -> {rule.get('new')!r}"
    if rtype in {"AST_CALL_REWRITE", "AST_ATTR_REWRITE"}:
        return (
            f"{rtype} {rule.get('old_callee') or rule.get('old_attr')} -> "
            f"{rule.get('new_callee') or rule.get('new_attr')}"
        )
    if rtype == "AST_PARAM_RENAME":
        return (
            f"{rtype} {rule.get('function_target')} "
            f"{rule.get('old_param')} -> {rule.get('new_param')}"
        )
    bits = [rtype]
    for key in ("old", "new", "old_callee", "new_callee", "package", "path"):
        if rule.get(key):
            bits.append(f"{key}={rule.get(key)}")
    return " ".join(bits)


def diff_packet_rules(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    cur_map: dict[str, dict[str, Any]] = {}
    for rule in current.get("rules") or []:
        if isinstance(rule, dict):
            cur_map[_rule_key(rule)] = rule
    prev_map: dict[str, dict[str, Any]] = {}
    for rule in (previous or {}).get("rules") or []:
        if isinstance(rule, dict):
            prev_map[_rule_key(rule)] = rule
    added = [cur_map[k] for k in cur_map if k not in prev_map]
    removed = [prev_map[k] for k in prev_map if k not in cur_map]
    return {"added": added, "removed": removed}


def create_packet_new(
    *,
    package: str,
    ecosystem: str,
    from_version: str,
    to_version: str,
    out: Path,
    enrich: bool = False,
    demo: bool = False,
    prefer_detect: bool = True,
    log=None,
) -> tuple[Path, dict[str, Any], list[str]]:
    """Build a hop packet via from-detect when possible, else scaffold."""
    warnings: list[str] = []
    package = package.strip()
    ecosystem = (ecosystem or "pypi").strip().lower()
    from_version = str(from_version).strip()
    to_version = str(to_version).strip()
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    packet: dict[str, Any] | None = None
    module = resolve_module_name(package) if prefer_detect else None
    if module:
        try:
            writes, det_warnings = run_packet_from_detect(
                module=module,
                out_dir=out.parent,
                package=package,
                ecosystem=ecosystem,
                previous_path=None,
                out_path=out,
                demo=demo,
                enrich=enrich,
                log=log,
            )
            warnings.extend(det_warnings)
            for item in writes:
                if item.packet and not item.skipped:
                    packet = item.packet
                    out = Path(item.path)
                    break
                if item.packet and item.skipped:
                    packet = item.packet
                    out = Path(item.path)
                    warnings.append(item.skip_reason or "snapshot already up to date")
                    break
        except ValueError as exc:
            warnings.append(str(exc))

    if packet is None:
        tmp_dir = out.parent / f".conduit-scaffold-{_safe_slug(package)}"
        scaffold_packet(
            package=package,
            ecosystem=ecosystem,
            from_version=from_version or "0",
            to_version=to_version,
            out_dir=tmp_dir,
        )
        src = tmp_dir / "conduit-packet.json"
        packet = json.loads(src.read_text(encoding="utf-8"))
        packet["from_version"] = from_version or packet.get("from_version") or "0"
        packet["to_version"] = to_version
        packet["ecosystem"] = ecosystem
        try:
            for child in tmp_dir.rglob("*"):
                if child.is_file():
                    child.unlink()
            for child in sorted(tmp_dir.rglob("*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
            if tmp_dir.exists():
                tmp_dir.rmdir()
        except OSError:
            pass
        if prefer_detect and module is None:
            warnings.append(
                f"no detect module for {package!r}; wrote scaffold packet"
            )

    if from_version:
        packet["from_version"] = from_version
    if to_version:
        packet["to_version"] = to_version
    packet.setdefault("package", package)
    packet.setdefault("ecosystem", ecosystem)

    errors = validate_packet(packet)
    if errors:
        warnings.extend(f"schema: {e}" for e in errors)

    out.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    return out, packet, warnings


def load_previous_for_diff(
    packet: dict[str, Any],
    *,
    previous_path: Path | None,
    search_dir: Path | None,
) -> dict[str, Any] | None:
    if previous_path is not None:
        return json.loads(previous_path.read_text(encoding="utf-8"))
    pkg = str(packet.get("package") or "")
    eco = str(packet.get("ecosystem") or "")
    to_v = str(packet.get("to_version") or "")
    if not (pkg and eco and to_v and search_dir is not None):
        return None
    return find_previous_snapshot(
        search_dir, package=pkg, ecosystem=eco, to_version=to_v
    )
