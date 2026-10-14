"""Compare a pinned outside-service client with a newer released surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from conduit.detect.manifests import read_installed
from conduit.feeds.versions import is_newer, parse_version
from conduit.impact.api import diff_wheels
from conduit.impact.botocore import diff_service_models
from conduit.impact.intersect import intersect
from conduit.impact.model import ApiChange, Impact
from conduit.impact.notes import changes_from_notes
from conduit.impact.webhook_schema import diff_webhook_schemas
from conduit.outside_in.catalog import CATALOG, catalog_spec
from conduit.outside_in.model import ServiceMap

if TYPE_CHECKING:
    from conduit.feeds.model import FeedHit


def service_pins(root: Path) -> dict[str, str]:
    """Pinned versions of outside-service clients only.

    Ordinary libraries in the same requirements file are ignored.
    """
    installed = read_installed(root)
    wanted = {
        spec.package.lower()
        for spec in CATALOG.values()
        if spec.package and not spec.family
    }
    pins: dict[str, str] = {}
    for name, version in installed.items():
        if name in wanted or name.startswith("google-cloud-") or name.startswith("azure-"):
            pins[name] = version
    return pins


NO_PIN_NOTE = "not compared: no pinned version found"


def collect_impacts(
    root: Path,
    service_map: ServiceMap,
    *,
    fixtures: Path | None = None,
    dep: str | None = None,
    to_version: str | None = None,
    from_version: str | None = None,
    hits: list[FeedHit] | None = None,
    allow_network: bool = False,
) -> tuple[list[Impact], set[str], list[str]]:
    """Return ``(impacts, diffed_packages, notes)``.

    ``diffed_packages`` is every client whose two surfaces were actually
    compared, including a diff that hit no call sites. Watch uses that to
    avoid a bare "new version" notice when the lines did not change.

    ``notes`` is set when the repo pin (or ``--from``) was not compared.
    That is not the same as a diff that hit no call sites.
    """
    index = _load_index(fixtures)
    explicit = bool(dep)
    targets = _targets(service_map, dep)
    pins = service_pins(root)
    impacts: list[Impact] = []
    diffed: set[str] = set()
    notes: list[str] = []
    for package, service_id in targets:
        entry = index.get(package.lower()) or index.get(service_id.lower()) or {}
        if not _should_run(package, entry, hits or [], explicit=explicit):
            continue
        resolved_from, resolved_to, version_note = _versions(
            package,
            pins,
            entry,
            hits or [],
            to_version if explicit else None,
            from_version if explicit else None,
            explicit=explicit,
        )
        if version_note and version_note not in notes:
            notes.append(version_note)
        changes, compared, compare_note = _changes_for(
            package,
            service_id,
            entry,
            fixtures,
            from_version=resolved_from,
            to_version=resolved_to,
            service_map=service_map,
            allow_network=allow_network and explicit,
            skip_versioned=bool(version_note),
            explicit=explicit,
        )
        if compare_note and compare_note not in notes:
            notes.append(compare_note)
        if compared:
            diffed.add(package.lower())
        if not changes or not resolved_to or resolved_to == resolved_from:
            webhook_only = [item for item in changes if item.source == "webhook"]
            if not webhook_only:
                continue
        version_changes = [item for item in changes if item.source != "webhook"]
        webhook_changes = [item for item in changes if item.source == "webhook"]
        if version_changes and resolved_to and resolved_to != resolved_from:
            impacts.extend(
                intersect(
                    version_changes,
                    service_map.chains,
                    dependency=package,
                    from_version=resolved_from,
                    to_version=resolved_to,
                )
            )
        if webhook_changes:
            hook_from, hook_to = _webhook_versions(entry)
            impacts.extend(
                intersect(
                    webhook_changes,
                    service_map.chains,
                    dependency=package,
                    from_version=hook_from,
                    to_version=hook_to,
                )
            )
    return impacts, diffed, notes


def _targets(service_map: ServiceMap, dep: str | None) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    wanted = (dep or "").strip().lower()
    for chain in service_map.chains:
        package = chain.package or chain.service
        if not package or package.lower() in seen:
            continue
        if wanted and wanted not in {package.lower(), chain.service.lower()}:
            spec = catalog_spec(wanted)
            if spec is None or spec.package.lower() != package.lower():
                continue
        seen.add(package.lower())
        found.append((package, chain.service))
    if wanted and not found:
        spec = catalog_spec(wanted)
        if spec is not None and not spec.family:
            found.append((spec.package, spec.service_id))
    return found


def _should_run(package: str, entry: dict, hits: list[FeedHit], *, explicit: bool) -> bool:
    if explicit:
        return True
    if not entry:
        return False
    for hit in hits:
        if hit.package.lower() == package.lower() and hit.change == "version" and hit.to_version:
            return True
    return bool(entry.get("webhook"))


def _versions(
    package: str,
    pins: dict[str, str],
    entry: dict,
    hits: list[FeedHit],
    to_override: str | None,
    from_override: str | None,
    *,
    explicit: bool,
) -> tuple[str, str, str]:
    """Resolve versions. Never invent a from-version from the fixture index."""
    chosen = (from_override or "").strip() or pins.get(package.lower(), "")
    if to_override:
        resolved_to = to_override
    else:
        hit_to = ""
        for hit in hits:
            if hit.package.lower() != package.lower() or hit.change != "version":
                continue
            if hit.to_version and _newer(hit.to_version, chosen):
                hit_to = hit.to_version
        resolved_to = hit_to or str(entry.get("to") or "")
    note = ""
    webhook_only = bool(entry.get("webhook")) and not _needs_library_version(entry)
    if not chosen and not webhook_only and (explicit or _needs_library_version(entry)):
        note = NO_PIN_NOTE
    return chosen, resolved_to, note


def _needs_library_version(entry: dict) -> bool:
    return bool(entry.get("wheels") or entry.get("botocore") or entry.get("changelog"))


def _newer(latest: str, current: str) -> bool:
    parsed = parse_version(latest)
    if parsed is None or not current:
        return bool(latest) and latest != current
    return is_newer(parsed, current)


def _webhook_versions(entry: dict) -> tuple[str, str]:
    block = entry.get("webhook") if isinstance(entry.get("webhook"), dict) else {}
    return str(block.get("from_version") or ""), str(block.get("to_version") or "")


def _changes_for(
    package: str,
    service_id: str,
    entry: dict,
    fixtures: Path | None,
    *,
    from_version: str,
    to_version: str,
    service_map: ServiceMap,
    allow_network: bool,
    skip_versioned: bool,
    explicit: bool,
) -> tuple[list[ApiChange], bool, str]:
    changes: list[ApiChange] = []
    compared = False
    note = ""
    wheel_compared = False
    if fixtures is None and not allow_network:
        if explicit and not skip_versioned and not _has_webhook(service_map, service_id, package):
            note = _missing_surface_note(from_version, to_version)
        return changes, compared, note
    recorded_from = str(entry.get("from") or "")
    wheels = entry.get("wheels") if isinstance(entry.get("wheels"), dict) else {}
    if not skip_versioned and from_version and to_version and from_version != to_version:
        if wheels or allow_network:
            old_wheel = _artifact(fixtures, wheels.get(from_version))
            new_wheel = _artifact(fixtures, wheels.get(to_version))
            if old_wheel is None and allow_network:
                old_wheel = _download_wheel(package, from_version)
            if new_wheel is None and allow_network:
                new_wheel = _download_wheel(package, to_version)
            if old_wheel is not None and new_wheel is not None:
                changes.extend(diff_wheels(old_wheel, new_wheel))
                compared = True
                wheel_compared = True
            elif wheels or (explicit and allow_network):
                note = _missing_surface_note(from_version, to_version, old_missing=old_wheel is None)
        botocore = entry.get("botocore") if isinstance(entry.get("botocore"), dict) else {}
        old_model = _artifact(fixtures, botocore.get("from"))
        new_model = _artifact(fixtures, botocore.get("to"))
        if old_model is not None and new_model is not None:
            if recorded_from and from_version != recorded_from:
                note = note or _missing_surface_note(from_version, to_version, old_missing=True)
            else:
                service = str(botocore.get("service") or "s3")
                changes.extend(diff_service_models(old_model, new_model, service=service))
                compared = True
        changelog = _artifact(fixtures, entry.get("changelog"))
        if changelog is not None and not note:
            version_ok = wheel_compared or (
                not wheels and (not recorded_from or from_version == recorded_from)
            )
            if version_ok:
                try:
                    text = changelog.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if text:
                    changes.extend(changes_from_notes(text))
                    compared = True
    elif not skip_versioned and explicit and (wheels or entry.get("botocore") or entry.get("changelog")):
        note = _missing_surface_note(from_version, to_version)
    if _has_webhook(service_map, service_id, package):
        hook = entry.get("webhook") if isinstance(entry.get("webhook"), dict) else {}
        old_schema = _artifact(fixtures, hook.get("from"))
        new_schema = _artifact(fixtures, hook.get("to"))
        if old_schema is not None and new_schema is not None:
            changes.extend(diff_webhook_schemas(old_schema, new_schema, provider=service_id))
            compared = True
    return changes, compared, note


def _missing_surface_note(from_version: str, to_version: str, *, old_missing: bool = True) -> str:
    if not from_version:
        return NO_PIN_NOTE
    if old_missing:
        return f"not compared: pinned version {from_version} does not match a recorded surface"
    if to_version:
        return f"not compared: version {to_version} does not match a recorded surface"
    return "not compared: no released surface to compare"


def _has_webhook(service_map: ServiceMap, service_id: str, package: str) -> bool:
    for chain in service_map.chains:
        if chain.kind == "webhook" and chain.service in {service_id, package}:
            return True
    return False


def _artifact(fixtures: Path | None, rel: object) -> Path | None:
    if fixtures is None or not rel:
        return None
    path = fixtures / str(rel)
    if path.is_file() or path.is_dir():
        return path
    return None


def _load_index(fixtures: Path | None) -> dict[str, dict]:
    if fixtures is None:
        return {}
    path = fixtures / "index.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    packages = payload.get("packages") if isinstance(payload, dict) else None
    if not isinstance(packages, dict):
        return {}
    return {str(key).lower(): value for key, value in packages.items() if isinstance(value, dict)}


def _download_wheel(package: str, version: str) -> Path | None:
    """Fetch one wheel from PyPI. Skips files larger than 12 MB."""
    if not package or not version:
        return None
    import tempfile

    import httpx

    meta_url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        response = httpx.get(meta_url, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        meta = response.json()
    except Exception:
        return None
    urls = meta.get("urls") if isinstance(meta, dict) else None
    if not isinstance(urls, list):
        return None
    wheels = [item for item in urls if isinstance(item, dict) and item.get("packagetype") == "bdist_wheel"]
    chosen = _prefer_wheel(wheels)
    if chosen is None:
        return None
    size = int(chosen.get("size") or 0)
    if size > 12_000_000:
        return None
    url = str(chosen.get("url") or "")
    filename = str(chosen.get("filename") or f"{package}-{version}.whl")
    if not url:
        return None
    dest = Path(tempfile.gettempdir()) / "conduit-wheels" / filename
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as streamed:
            streamed.raise_for_status()
            total = 0
            with dest.open("wb") as handle:
                for chunk in streamed.iter_bytes():
                    total += len(chunk)
                    if total > 12_000_000:
                        handle.close()
                        dest.unlink(missing_ok=True)
                        return None
                    handle.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        return None
    return dest


def _prefer_wheel(wheels: list[dict]) -> dict | None:
    if not wheels:
        return None

    def rank(item: dict) -> tuple[int, str]:
        name = str(item.get("filename") or "")
        py3 = 0 if "py3" in name or "py2.py3" in name else 1
        return py3, name

    return sorted(wheels, key=rank)[0]
