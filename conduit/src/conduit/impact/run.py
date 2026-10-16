"""Compare a pinned outside-service client with a newer released surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from conduit.deps.manifests import read_installed
from conduit.feeds.versions import is_newer, parse_version
from conduit.impact.api import diff_wheels, public_identifiers
from conduit.impact.botocore import diff_service_models
from conduit.impact.shipped_specs import diff_shipped_specs
from conduit.impact.intersect import intersect
from conduit.impact.model import ApiChange, Impact, SiteHit
from conduit.impact.notes import changes_from_notes
from conduit.impact.openapi_rest import PUBLIC_SPEC_URLS, diff_openapi, split_http_target
from conduit.impact.openapi_sources import resolve_spec_pair
from conduit.impact.versions import (
    lowest_on_pypi,
    pypi_latest,
    resolve_client_version,
    unbounded_lower_spec,
    wheel_backed_version,
)
from conduit.impact.webhook_schema import diff_webhook_schemas
from conduit.outside_in.specs import evidence_spec, normalize_dist
from conduit.outside_in.model import Chain, ServiceMap

if TYPE_CHECKING:
    from conduit.feeds.model import FeedHit


def service_pins(root: Path, *, only: set[str] | None = None) -> dict[str, str]:
    """Pinned versions of outside-service clients only.

    Ordinary libraries in the same requirements file are ignored.
    ``only`` limits classification to packages the map already reached.
    """
    installed = read_installed(root)
    allowed = {normalize_dist(name) for name in only} if only is not None else None
    pins: dict[str, str] = {}
    for name, version in installed.items():
        norm = normalize_dist(name)
        if allowed is not None and norm not in allowed:
            continue
        spec = evidence_spec(name, version, root=root)
        if spec is None or spec.transport or spec.family:
            continue
        pins[normalize_dist(spec.package or name)] = version
    return pins


NO_PIN_NOTE = (
    "not compared: no version could be determined "
    "(no exact pin, lockfile entry, installed distribution, or resolvable requirement)"
)


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
    targets = _targets(service_map, dep, root=root)
    names = {package for package, _service in targets}
    if dep:
        names.add(dep)
    pins = service_pins(root, only=names)
    impacts: list[Impact] = []
    diffed: set[str] = set()
    notes: list[str] = []
    for package, service_id in targets:
        entry = index.get(package.lower()) or index.get(service_id.lower()) or {}
        if not _should_run(
            package,
            entry,
            hits or [],
            explicit=explicit,
            webhook=_has_webhook(service_map, service_id, package),
            allow_network=allow_network,
        ):
            continue
        resolved_from, resolved_to, version_note = _versions(
            root,
            package,
            pins,
            entry,
            hits or [],
            to_version if explicit else None,
            from_version if explicit else None,
            explicit=explicit,
            allow_network=allow_network,
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
            allow_network=allow_network,
            skip_versioned=bool(version_note),
            explicit=explicit,
        )
        for line in (compare_note or "").splitlines():
            if line and line not in notes:
                notes.append(line)
        if compared:
            diffed.add(package.lower())
        version_changes = [item for item in changes if item.source != "webhook" and not _is_rest(item)]
        webhook_changes = [item for item in changes if item.source == "webhook"]
        rest_changes = [item for item in changes if _is_rest(item)]
        if not resolved_to or resolved_to == resolved_from:
            version_changes = []
        if version_changes and resolved_to and resolved_to != resolved_from:
            impacts.extend(
                intersect(
                    version_changes,
                    service_map.chains,
                    dependency=package,
                    from_version=resolved_from,
                    to_version=resolved_to,
                    return_classes=getattr(changes, "return_classes", None),
                    surviving=getattr(changes, "surviving", None),
                    methods=getattr(changes, "methods", None),
                )
            )
        if rest_changes:
            impacts.extend(
                intersect(
                    rest_changes,
                    service_map.chains,
                    dependency=package,
                    from_version=resolved_from,
                    to_version=resolved_to or "latest",
                )
            )
        if resolved_from and resolved_to and resolved_to != resolved_from:
            impacts.extend(
                _version_gate_impacts(
                    service_map.chains,
                    package=package,
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


def _targets(
    service_map: ServiceMap,
    dep: str | None,
    *,
    root: Path | None = None,
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    wanted = (dep or "").strip().lower()
    wanted_spec = evidence_spec(wanted, root=root) if wanted else None
    for chain in service_map.chains:
        package = chain.package or chain.service
        if not package or package.lower() in seen:
            continue
        if wanted and wanted not in {package.lower(), chain.service.lower()}:
            if wanted_spec is None or wanted_spec.package.lower() != package.lower():
                continue
        seen.add(package.lower())
        found.append((package, chain.service))
    if wanted and not found:
        spec = evidence_spec(wanted, root=root)
        if spec is not None and not spec.family:
            found.append((spec.package, spec.service_id))
    return found


def _should_run(
    package: str,
    entry: dict,
    hits: list[FeedHit],
    *,
    explicit: bool,
    webhook: bool = False,
    allow_network: bool = False,
) -> bool:
    if explicit:
        return True
    for hit in hits:
        if hit.package.lower() == package.lower() and hit.change == "version" and hit.to_version:
            return True
    if entry.get("webhook"):
        return True
    # A live watch has no recorded webhook schema. Still run so the miss is reported.
    return bool(allow_network and webhook and not entry)


def _versions(
    root: Path,
    package: str,
    pins: dict[str, str],
    entry: dict,
    hits: list[FeedHit],
    to_override: str | None,
    from_override: str | None,
    *,
    explicit: bool,
    allow_network: bool,
) -> tuple[str, str, str]:
    """Resolve versions. Never invent a from-version from the fixture index."""
    note = ""
    chosen = (from_override or "").strip()
    if not chosen:
        chosen, reason = resolve_client_version(root, package, allow_network=allow_network)
        if not chosen:
            pinned = pins.get(package.lower(), "")
            if pinned and _exact_looking(pinned):
                chosen = pinned
            else:
                note = reason or NO_PIN_NOTE
    if to_override:
        resolved_to = to_override
    else:
        hit_to = ""
        for hit in hits:
            if hit.package.lower() != package.lower() or hit.change != "version":
                continue
            if hit.to_version and (
                _newer(hit.to_version, chosen) or _same_version(hit.to_version, chosen)
            ):
                hit_to = hit.to_version
        resolved_to = hit_to or str(entry.get("to") or "")
        if not resolved_to and explicit and allow_network and chosen:
            try:
                resolved_to = pypi_latest(package)
            except Exception:
                resolved_to = ""
            if not resolved_to:
                note = note or f"not compared: could not read the latest {package} version from PyPI"
        if allow_network and resolved_to:
            try:
                backed = wheel_backed_version(package, resolved_to)
            except Exception:
                backed = ""
            if backed and not _same_version(backed, resolved_to):
                resolved_to = backed
        elif not allow_network:
            recorded_wheels = entry.get("wheels") if isinstance(entry.get("wheels"), dict) else {}
            recorded_to = str(entry.get("to") or "")
            if resolved_to not in recorded_wheels and recorded_to in recorded_wheels:
                resolved_to = recorded_to
    if (
        allow_network
        and not from_override
        and chosen
        and resolved_to
        and _same_version(chosen, resolved_to)
    ):
        # An open lower bound resolves to the newest release, which is also
        # the version being watched. Compare from the oldest release the
        # requirement allows so the two published surfaces can differ.
        floor_spec = unbounded_lower_spec(root, package)
        if floor_spec:
            try:
                floor = lowest_on_pypi(package, floor_spec)
            except Exception:
                floor = ""
            if floor and not _same_version(floor, chosen):
                chosen = floor
    webhook_only = bool(entry.get("webhook")) and not _needs_library_version(entry)
    if chosen and note == NO_PIN_NOTE:
        note = ""
    if not chosen and webhook_only:
        note = ""
    elif not chosen and not (explicit or _needs_library_version(entry)):
        note = ""
    elif not chosen:
        note = note or NO_PIN_NOTE
    return chosen, resolved_to, note


def _same_version(left: str, right: str) -> bool:
    from packaging.version import InvalidVersion, Version

    try:
        return Version(left) == Version(right)
    except InvalidVersion:
        return (left or "") == (right or "")


def _exact_looking(value: str) -> bool:
    token = (value or "").strip()
    return bool(token) and not any(op in token for op in (">", "<", "!", "~", "^", ",", "*"))


def _needs_library_version(entry: dict) -> bool:
    return bool(entry.get("wheels") or entry.get("service_model") or entry.get("changelog"))


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
    changes = _ChangeList()
    compared = False
    note = ""
    wheel_compared = False
    if fixtures is None and not allow_network:
        if explicit and not skip_versioned and not _has_webhook(service_map, service_id, package):
            note = _missing_surface_note(from_version, to_version)
        return changes, compared, note
    recorded_from = str(entry.get("from") or "")
    wheels = entry.get("wheels") if isinstance(entry.get("wheels"), dict) else {}
    old_wheel: Path | None = None
    new_wheel: Path | None = None
    if not skip_versioned and from_version and to_version and from_version != to_version:
        if wheels or allow_network:
            old_wheel = _artifact(fixtures, wheels.get(from_version))
            new_wheel = _artifact(fixtures, wheels.get(to_version))
            if old_wheel is None and allow_network:
                old_wheel = _download_wheel(package, from_version)
            if new_wheel is None and allow_network:
                new_wheel = _download_wheel(package, to_version)
            if old_wheel is not None and new_wheel is not None:
                wheel_diff = diff_wheels(old_wheel, new_wheel)
                changes.extend(wheel_diff)
                changes.return_classes = getattr(wheel_diff, "return_classes", {}) or {}
                changes.methods = getattr(wheel_diff, "methods", {}) or {}
                changes.surviving = getattr(wheel_diff, "surviving", set()) or set()
                compared = True
                wheel_compared = True
                spec_changes, spec_compared, spec_note = diff_shipped_specs(
                    old_wheel,
                    new_wheel,
                    package=package,
                    chains=service_map.chains,
                    allow_network=allow_network,
                    search_dirs=[fixtures] if fixtures is not None else [],
                )
                changes.extend(spec_changes)
                if spec_compared:
                    compared = True
                if spec_note:
                    note = f"{note}\n{spec_note}" if note else spec_note
            elif wheels:
                note = _missing_surface_note(from_version, to_version, old_missing=old_wheel is None)
            elif allow_network:
                note = _unfetched_wheel_note(
                    package,
                    from_version,
                    to_version,
                    old_missing=old_wheel is None,
                    new_missing=new_wheel is None,
                )
        service_model = entry.get("service_model") if isinstance(entry.get("service_model"), dict) else {}
        old_model = _artifact(fixtures, service_model.get("from"))
        new_model = _artifact(fixtures, service_model.get("to"))
        if old_model is not None and new_model is not None:
            if recorded_from and from_version != recorded_from:
                note = note or _missing_surface_note(from_version, to_version, old_missing=True)
            else:
                service = str(service_model.get("service") or "s3")
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
                    noted = [
                        item
                        for item in changes_from_notes(text)
                        if item.kind not in {"endpoint_removed", "endpoint_renamed"}
                    ]
                    names = _identifiers(old_wheel, new_wheel)
                    if names:
                        from conduit.feeds.names import rename_grounded

                        noted = [
                            item
                            for item in noted
                            if item.kind not in {"renamed", "param_renamed"}
                            or rename_grounded(item.old, item.new, names)
                        ]
                    changes.extend(noted)
                    compared = True
    elif not skip_versioned and explicit and (wheels or entry.get("service_model") or entry.get("changelog")):
        note = _missing_surface_note(from_version, to_version)
    if _has_webhook(service_map, service_id, package):
        hook = entry.get("webhook") if isinstance(entry.get("webhook"), dict) else {}
        old_schema = _artifact(fixtures, hook.get("from"))
        new_schema = _artifact(fixtures, hook.get("to"))
        if old_schema is not None and new_schema is not None:
            changes.extend(diff_webhook_schemas(old_schema, new_schema, provider=service_id))
            compared = True
        elif fixtures is None and allow_network:
            hook_note = f"not compared: no versioned webhook schema for {package} could be fetched"
            note = f"{note}\n{hook_note}" if note else hook_note
    rest, rest_note, rest_compared = _rest_changes(
        package,
        service_id,
        entry,
        fixtures,
        service_map=service_map,
        allow_network=allow_network,
        from_version=from_version,
        to_version=to_version,
    )
    changes.extend(rest)
    if rest_compared:
        compared = True
    if rest_note:
        note = f"{note}\n{rest_note}" if note else rest_note
    return changes, compared, note


class _ChangeList(list):
    """Wheel diff facts the matcher needs, kept when other sources are appended."""

    return_classes: dict
    methods: dict
    surviving: set

    def __init__(self) -> None:
        super().__init__()
        self.return_classes = {}
        self.methods = {}
        self.surviving = set()


def _is_rest(change: ApiChange) -> bool:
    return change.source == "openapi" or change.kind in {"endpoint_removed", "endpoint_renamed"}


def _version_gate_impacts(
    chains: tuple[Chain, ...] | list[Chain],
    *,
    package: str,
    from_version: str,
    to_version: str,
) -> list[Impact]:
    key = package.lower()
    sites: list[SiteHit] = []
    seen: set[tuple[str, int, str]] = set()
    for chain in chains:
        if chain.kind != "version_gate":
            continue
        if chain.package.lower() != key and chain.service.lower() != key:
            continue
        marker = (chain.call_rel, chain.call_lineno, chain.call_expr)
        if marker in seen:
            continue
        seen.add(marker)
        sites.append(
            SiteHit(
                path=chain.call_rel,
                line=chain.call_lineno,
                expr=chain.call_expr,
                confidence=chain.confidence,
                via_wrapper=False,
            )
        )
    if not sites:
        return []
    return [
        Impact(
            dependency=package,
            from_version=from_version,
            to_version=to_version,
            change=f"code branches on {package} version",
            confidence="high",
            source="version",
            sites=tuple(sites),
        )
    ]


def _rest_changes(
    package: str,
    service_id: str,
    entry: dict,
    fixtures: Path | None,
    *,
    service_map: ServiceMap,
    allow_network: bool,
    from_version: str = "",
    to_version: str = "",
) -> tuple[list[ApiChange], str, bool]:
    """OpenAPI pair when present, otherwise changelog hints and an honest miss."""
    calls = [
        chain
        for chain in service_map.chains
        if chain.kind == "http_call" and _same_package(chain, package, service_id)
    ]
    if not calls:
        return [], "", False
    spec = entry.get("openapi") if isinstance(entry.get("openapi"), dict) else {}
    old_spec = _artifact(fixtures, spec.get("from"))
    new_spec = _artifact(fixtures, spec.get("to"))
    if old_spec is not None and new_spec is not None:
        return diff_openapi(old_spec, new_spec, service=service_id), "", True
    fetch_note = ""
    if allow_network and from_version and to_version and from_version != to_version:
        fetched_old, fetched_new, fetch_note = resolve_spec_pair(
            service_id,
            from_ref=from_version,
            to_ref=to_version,
            allow_network=True,
            package=package,
        )
        if fetched_old is not None and fetched_new is not None:
            return diff_openapi(fetched_old, fetched_new, service=service_id), "", True
    changes: list[ApiChange] = []
    changelog = _artifact(fixtures, entry.get("changelog"))
    if changelog is not None:
        try:
            text = changelog.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text:
            changes.extend(
                item
                for item in changes_from_notes(text)
                if item.kind in {"endpoint_removed", "endpoint_renamed"}
            )
    uncovered = _uncovered_rest(calls, changes)
    if fetch_note:
        note = fetch_note
        if changes:
            note = note + " Changelog text was used only as a hint."
        return changes, note, bool(changes)
    if old_spec is None and new_spec is None and allow_network and service_id.lower() in PUBLIC_SPEC_URLS:
        note = _public_spec_note(service_id, uncovered or calls)
        return changes, note, bool(changes)
    if not uncovered and changes:
        return changes, "", True
    note = _no_spec_note(uncovered or calls)
    if changes:
        note = note + " Changelog text was used only as a hint."
    return changes, note, bool(changes)


def _same_package(chain: Chain, package: str, service_id: str) -> bool:
    key = {package.lower(), service_id.lower()}
    return chain.package.lower() in key or chain.service.lower() in key


def _uncovered_rest(calls: list[Chain], changes: list[ApiChange]) -> list[Chain]:
    from conduit.impact.openapi_rest import paths_match

    covered: list[Chain] = []
    for chain in calls:
        _method, path = split_http_target(chain.sdk_method)
        if any(paths_match(path, change.old) or paths_match(path, change.symbol.split(" ", 1)[-1]) for change in changes):
            covered.append(chain)
    return [chain for chain in calls if chain not in covered]


def _no_spec_note(calls: list[Chain]) -> str:
    shown = []
    for chain in calls:
        label = chain.sdk_method or f"{chain.call_rel}:{chain.call_lineno}"
        if label not in shown:
            shown.append(label)
    preview = ", ".join(shown[:4])
    extra = f" (+{len(shown) - 4} more)" if len(shown) > 4 else ""
    return (
        f"not compared: {len(shown)} REST call(s) had no OpenAPI spec to compare ({preview}{extra})"
    )


def _public_spec_note(service_id: str, calls: list[Chain]) -> str:
    url = PUBLIC_SPEC_URLS.get(service_id.lower(), "")
    shown = []
    for chain in calls:
        label = chain.sdk_method
        if label and label not in shown:
            shown.append(label)
    preview = ", ".join(shown[:3])
    return (
        f"not compared: {service_id} REST call(s) ({preview}) had no versioned OpenAPI pair. "
        f"The public spec repo is {url}; a latest document alone is not a diff."
    )


def _unfetched_wheel_note(
    package: str,
    from_version: str,
    to_version: str,
    *,
    old_missing: bool,
    new_missing: bool,
) -> str:
    missed: list[str] = []
    if old_missing and from_version:
        missed.append(from_version)
    if new_missing and to_version and to_version not in missed:
        missed.append(to_version)
    listed = ", ".join(missed) if missed else "unknown"
    return f"not compared: could not fetch {package} wheel(s) {listed}"


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


def _identifiers(*wheels: Path | None) -> set[str]:
    present = [wheel for wheel in wheels if wheel is not None]
    if not present:
        return set()
    return public_identifiers(present)


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


def _download_wheel(package: str, version: str, *, max_bytes: int = 32_000_000) -> Path | None:
    """Fetch one wheel from PyPI. Skips files larger than ``max_bytes``."""
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
    if size > max_bytes:
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
                    if total > max_bytes:
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
