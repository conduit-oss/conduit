"""Diff versioned API specs shipped inside a package and its dependencies.

The classifier treats a JSON or YAML member as a versioned spec. Those files
often live in a dependency wheel rather than the client wheel. This module
finds them by reading archive contents and ``Requires-Dist`` pins. It does
not name any distribution.
"""

from __future__ import annotations

import gzip
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from conduit.impact.model import ApiChange
from conduit.outside_in.evidence import _spec_kind
from conduit.outside_in.specs import normalize_dist

#: Dependency wheels that carry spec documents are larger than client wheels.
SPEC_WHEEL_MAX_BYTES = 32_000_000  # same ceiling as the client wheel download
_SPEC_MEMBER_MAX_BYTES = 8_000_000


def diff_shipped_specs(
    old_wheel: Path,
    new_wheel: Path,
    *,
    package: str,
    chains,
    allow_network: bool,
    search_dirs: list[Path] | None = None,
) -> tuple[list[ApiChange], bool, str]:
    """Return ``(changes, compared, note)`` for specs inside the two wheels.

    ``compared`` is true when every called service that ships a spec was
    paired, including a diff that removed nothing. A dependency wheel that
    cannot be fetched is reported and does not count as compared.
    """
    wanted = called_service_names(chains, package)
    if not wanted:
        return [], False, ""
    dirs = [path for path in (search_dirs or []) if path is not None]
    notes: list[str] = []
    old_specs, old_notes = _specs_for_wheel(
        old_wheel, allow_network=allow_network, search_dirs=dirs, wanted=wanted
    )
    new_specs, new_notes = _specs_for_wheel(
        new_wheel, allow_network=allow_network, search_dirs=dirs, wanted=wanted
    )
    notes.extend(old_notes)
    notes.extend(new_notes)
    old_best = _best_by_identity(old_specs)
    new_best = _best_by_identity(new_specs)
    changes: list[ApiChange] = []
    paired = False
    missing: list[str] = []
    for service in sorted(wanted):
        previous = old_best.get(service)
        latest = new_best.get(service)
        if previous is None or latest is None:
            if previous is not None or latest is not None:
                missing.append(service)
            continue
        paired = True
        changes.extend(_diff_pair(previous, latest, service=service))
    if missing:
        listed = ", ".join(missing)
        notes.append(f"not compared: no versioned spec for {listed} in {package}")
    note = "\n".join(dict.fromkeys(item for item in notes if item))
    if notes and not paired:
        return changes, False, note
    return changes, paired, note


def called_service_names(chains, package: str) -> set[str]:
    """Service tokens on cloud calls for ``package`` (the text before the dot)."""
    key = (package or "").lower()
    found: set[str] = set()
    for chain in chains:
        if getattr(chain, "kind", "") not in {"cloud_call", "cloud_constructor"}:
            continue
        owner = {(getattr(chain, "package", "") or "").lower(), (getattr(chain, "service", "") or "").lower()}
        if key not in owner:
            continue
        method = getattr(chain, "sdk_method", "") or ""
        if "." not in method:
            continue
        token = method.split(".", 1)[0].strip().lower()
        if token:
            found.add(token)
    return found


def closed_pins(metadata: str) -> list[tuple[str, str]]:
    """``(name, version)`` for exact pins and closed ranges.

    ``==`` wins. A range with both a lower and an upper bound uses the lower
    bound. Extras are skipped. An open range is not a pin.
    """
    from packaging.requirements import InvalidRequirement, Requirement

    pins: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in (metadata or "").splitlines():
        if not line.lower().startswith("requires-dist:"):
            continue
        body = line.split(":", 1)[1].strip()
        if not body:
            continue
        try:
            req = Requirement(body)
        except InvalidRequirement:
            continue
        marker = str(req.marker or "")
        if "extra" in marker:
            continue
        version = _pin_version(req)
        if not version or "*" in version:
            continue
        name = normalize_dist(req.name)
        if not name or name in seen or name == "python":
            continue
        seen.add(name)
        pins.append((name, version))
    return pins


def read_wheel_metadata(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.filename.lower().endswith(".dist-info/metadata"):
                    return archive.read(info).decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile):
        return ""
    return ""


def wheel_identity(path: Path) -> tuple[str, str]:
    text = read_wheel_metadata(path)
    name = ""
    version = ""
    for line in text.splitlines():
        lowered = line.lower()
        if lowered.startswith("name:") and not name:
            name = line.split(":", 1)[1].strip()
        elif lowered.startswith("version:") and not version:
            version = line.split(":", 1)[1].strip()
        if name and version:
            break
    if name and version:
        return normalize_dist(name), version
    return "", ""


def iter_spec_members(path: Path) -> list[tuple[str, str, bytes]]:
    """``(archive name, kind, decoded body)`` for versioned spec members."""
    found: list[tuple[str, str, bytes]] = []
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        return found
    with archive:
        for info in archive.infolist():
            if info.is_dir() or info.file_size > _SPEC_MEMBER_MAX_BYTES:
                continue
            name = info.filename
            lowered = name.lower()
            if not (
                lowered.endswith("service-2.json")
                or lowered.endswith("service-2.json.gz")
                or lowered.endswith((".json", ".yaml", ".yml"))
            ):
                continue
            try:
                raw = archive.read(info)
            except Exception:
                continue
            body = raw
            if lowered.endswith(".gz"):
                try:
                    body = gzip.decompress(raw)
                except Exception:
                    continue
            kind = _spec_kind(name, body[:4000])
            if not kind:
                continue
            found.append((name, kind, body))
    return found


def write_spec_snapshot(source: Path, dest: Path, services: set[str]) -> int:
    """Copy metadata and spec members for ``services`` into a new zip.

    The member bytes are the published bytes. Returns how many spec members
    were written. ``dest`` is removed when none match.
    """
    if not services:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile):
        return 0
    with archive, zipfile.ZipFile(dest, "w") as out:
        for info in archive.infolist():
            lower = info.filename.lower()
            if lower.endswith(".dist-info/metadata"):
                out.writestr(info, archive.read(info))
                continue
            if info.is_dir() or info.file_size > _SPEC_MEMBER_MAX_BYTES:
                continue
            if not (
                lower.endswith("service-2.json")
                or lower.endswith("service-2.json.gz")
                or lower.endswith((".json", ".yaml", ".yml"))
            ):
                continue
            try:
                raw = archive.read(info)
            except Exception:
                continue
            body = raw
            if lower.endswith(".gz"):
                try:
                    body = gzip.decompress(raw)
                except Exception:
                    continue
            kind = _spec_kind(info.filename, body[:4000])
            if not kind:
                continue
            if _identity(info.filename, kind, body) not in services:
                continue
            out.writestr(info, raw)
            count += 1
    if count == 0:
        dest.unlink(missing_ok=True)
    return count


def locate_wheel(package: str, version: str, search_dirs: list[Path]) -> Path | None:
    """A wheel under ``search_dirs`` whose metadata name and version match."""
    wanted = normalize_dist(package)
    if not wanted or not version:
        return None
    for directory in search_dirs:
        if directory is None or not directory.exists():
            continue
        candidates = [directory] if directory.is_file() else list(directory.rglob("*.whl"))
        for path in candidates:
            if not path.is_file() or path.suffix != ".whl":
                continue
            name, found = wheel_identity(path)
            if name == wanted and found == version:
                return path
    return None


def _specs_for_wheel(
    wheel: Path,
    *,
    allow_network: bool,
    search_dirs: list[Path],
    wanted: set[str],
) -> tuple[list[tuple[str, str, bytes]], list[str]]:
    specs = list(iter_spec_members(wheel))
    notes: list[str] = []
    for name, version in closed_pins(read_wheel_metadata(wheel)):
        dep = locate_wheel(name, version, search_dirs)
        if dep is None and allow_network:
            from conduit.impact.run import _download_wheel

            dep = _download_wheel(name, version, max_bytes=SPEC_WHEEL_MAX_BYTES)
        if dep is None:
            notes.append(f"not compared: could not fetch {name} wheel(s) {version}")
            continue
        specs.extend(iter_spec_members(dep))
    # A dependency that ships no spec is not a failed comparison once every
    # called service already has a document.
    covered = set(_best_by_identity(specs))
    if wanted and wanted <= covered:
        return specs, []
    return specs, notes


def _best_by_identity(
    specs: list[tuple[str, str, bytes]],
) -> dict[str, tuple[str, str, bytes]]:
    grouped: dict[str, list[tuple[str, str, bytes]]] = {}
    for item in specs:
        identity = _identity(item[0], item[1], item[2])
        if not identity:
            continue
        grouped.setdefault(identity, []).append(item)
    chosen: dict[str, tuple[str, str, bytes]] = {}
    for identity, items in grouped.items():
        chosen[identity] = max(items, key=lambda item: (_api_version(item[0]), len(item[2])))
    return chosen


def _identity(path: str, kind: str, body: bytes) -> str:
    if kind == "service-model":
        payload = _json_object(body)
        meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        token = str(meta.get("endpointPrefix") or meta.get("serviceId") or "").strip()
        if token:
            return token.lower()
    parts = [part for part in PurePosixPath(path).parts[:-1] if part not in {"", "."}]
    for part in reversed(parts):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            continue
        if part.lower() in {"data", "models"}:
            continue
        return part.lower()
    return PurePosixPath(path).stem.lower()


def _api_version(path: str) -> str:
    for part in PurePosixPath(path).parts:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            return part
    return ""


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _diff_pair(
    previous: tuple[str, str, bytes],
    latest: tuple[str, str, bytes],
    *,
    service: str,
) -> list[ApiChange]:
    import tempfile

    old_kind = previous[1]
    new_kind = latest[1]
    if old_kind != new_kind:
        return []
    suffix = ".json" if old_kind == "service-model" or previous[0].lower().endswith(".json") else ".yaml"
    with tempfile.TemporaryDirectory(prefix="conduit-spec-") as directory:
        root = Path(directory)
        old_path = root / f"old{suffix}"
        new_path = root / f"new{suffix}"
        old_path.write_bytes(previous[2])
        new_path.write_bytes(latest[2])
        if old_kind == "service-model":
            from conduit.impact.botocore import diff_service_models

            return diff_service_models(old_path, new_path, service=service)
        from conduit.impact.openapi_rest import diff_openapi

        return diff_openapi(old_path, new_path, service=service)


def _pin_version(req) -> str:
    specs = list(req.specifier)
    for spec in specs:
        if spec.operator in {"==", "==="}:
            return spec.version
    if any(spec.operator == "~=" for spec in specs):
        for spec in specs:
            if spec.operator == "~=":
                return spec.version
    has_upper = any(spec.operator in {"<", "<="} for spec in specs)
    if not has_upper:
        return ""
    floors = [spec.version for spec in specs if spec.operator == ">="]
    if not floors:
        floors = [spec.version for spec in specs if spec.operator == ">"]
    return floors[0] if floors else ""
