"""OpenAPIDiffWorker: diff vendor OpenAPI specs for breaking changes."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from conduit.detect.modules.openai.models_legacy import ChangeType, RawSignal, Severity
from conduit.detect.modules.openai.path_callees import callees_for_path
from conduit.detect.modules.openai.workers.base import Worker, fixtures_dir

OPENAPI_SOURCE_URL = "https://github.com/openai/openai-openapi"

# Module-level cache so path_param_compat can reuse a live clone within one process run.
_OPENAPI_PAIR_CACHE: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}


@dataclass
class PathParamDiff:
    renames: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


def _load_openapi(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").lstrip("\ufeff").strip()
    if not text:
        raise ValueError(f"empty OpenAPI file: {path}")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"} or text[:1] not in "[{":
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"OpenAPI root must be a mapping: {path}")
    return data


def _request_props(path_item: dict[str, Any]) -> set[str]:
    props: set[str] = set()
    for method, op in path_item.items():
        if method.startswith("x-") or not isinstance(op, dict):
            continue
        body = (op.get("requestBody") or {}).get("content") or {}
        for media in body.values():
            schema = media.get("schema") or {}
            props.update((schema.get("properties") or {}).keys())
    return props


def _props_for_path(spec: dict[str, Any], path: str) -> set[str]:
    item = (spec.get("paths") or {}).get(path)
    if not isinstance(item, dict):
        return set()
    return _request_props(item)


def diff_path_params(
    previous: dict[str, Any],
    latest: dict[str, Any],
    old_path: str,
    new_path: str,
) -> PathParamDiff:
    """Compare request-body props on previous[old_path] vs latest[new_path]."""
    prev_props = _props_for_path(previous, old_path)
    latest_props = _props_for_path(latest, new_path)
    removed_props = prev_props - latest_props
    added_props = latest_props - prev_props
    result = PathParamDiff()
    if len(removed_props) == 1 and len(added_props) == 1:
        old_p = next(iter(removed_props))
        new_p = next(iter(added_props))
        result.renames.append((old_p, new_p))
    else:
        result.removed = sorted(removed_props)
        result.added = sorted(added_props)
    return result


def load_openapi_pair(
    *,
    demo: bool = False,
    cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Load (previous, latest) OpenAPI specs. Demo uses fixtures; live clones latest."""
    store = cache if cache is not None else _OPENAPI_PAIR_CACHE
    key = "demo" if demo else "live"
    if key in store:
        return store[key]

    fixture_prev = fixtures_dir() / "openapi" / "previous.yaml"
    fixture_latest = fixtures_dir() / "openapi" / "latest.yaml"
    if not fixture_prev.is_file():
        return None

    previous = _load_openapi(fixture_prev)
    if demo:
        if not fixture_latest.is_file():
            return None
        latest = _load_openapi(fixture_latest)
        store[key] = (previous, latest)
        return store[key]

    # Live: prefer cache filled by OpenAPIDiffWorker; else clone tip once.
    repo = "https://github.com/openai/openai-openapi.git"
    with tempfile.TemporaryDirectory(prefix="oasdiff-") as tmp:
        tmp_path = Path(tmp)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", repo, str(tmp_path / "repo")],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if fixture_latest.is_file():
                latest = _load_openapi(fixture_latest)
                store[key] = (previous, latest)
                return store[key]
            return None
        repo_path = tmp_path / "repo"
        candidates = list(repo_path.glob("**/openapi.y*ml")) + list(
            repo_path.glob("**/openapi.json")
        )
        if not candidates:
            if fixture_latest.is_file():
                latest = _load_openapi(fixture_latest)
                store[key] = (previous, latest)
                return store[key]
            return None
        latest = _load_openapi(candidates[0])
        store[key] = (previous, latest)
        return store[key]


def _param_rename_signal(
    *,
    path: str,
    old_path: str,
    new_path: str,
    old_p: str,
    new_p: str,
) -> RawSignal:
    targets = callees_for_path(new_path)
    reason = (
        f"Request property renamed on {old_path}"
        + (f" → {new_path}" if old_path != new_path else "")
        + f": {old_p} → {new_p}. Source: {OPENAPI_SOURCE_URL}"
    )
    extra: dict[str, Any] = {
        "old_param": old_p,
        "new_param": new_p,
        "path": path,
        "old_path": old_path,
        "new_path": new_path,
        "reason": reason,
    }
    if targets:
        extra["function_targets"] = targets
        extra["function_target"] = targets[0]
    return RawSignal(
        vendor="openai",
        change_type=ChangeType.PARAM_RENAME,
        severity=Severity.CRITICAL,
        affected_pattern=old_p,
        replacement_pattern=new_p,
        source_url=OPENAPI_SOURCE_URL,
        description=reason,
        extra=extra,
    )


def _diff_paths(previous: dict[str, Any], latest: dict[str, Any]) -> list[RawSignal]:
    signals: list[RawSignal] = []
    prev_paths = set((previous.get("paths") or {}).keys())
    latest_paths = set((latest.get("paths") or {}).keys())

    for removed in sorted(prev_paths - latest_paths):
        signals.append(
            RawSignal(
                vendor="openai",
                change_type=ChangeType.API_BREAKING,
                severity=Severity.CRITICAL,
                affected_pattern=removed,
                replacement_pattern=None,
                source_url=OPENAPI_SOURCE_URL,
                description=f"OpenAPI path removed: {removed}",
                suggested_rules=[],
            )
        )

    # Detect renamed request-body properties on shared paths
    for path in sorted(prev_paths & latest_paths):
        diff = diff_path_params(previous, latest, path, path)
        if diff.renames:
            for old_p, new_p in diff.renames:
                signals.append(
                    _param_rename_signal(
                        path=path,
                        old_path=path,
                        new_path=path,
                        old_p=old_p,
                        new_p=new_p,
                    )
                )
        else:
            for prop in diff.removed:
                signals.append(
                    RawSignal(
                        vendor="openai",
                        change_type=ChangeType.API_BREAKING,
                        severity=Severity.WARNING,
                        affected_pattern=prop,
                        source_url=OPENAPI_SOURCE_URL,
                        description=f"Request property removed on {path}: {prop}",
                        extra={"path": path},
                    )
                )
    return signals


def _run_oasdiff(prev: Path, latest: Path) -> list[RawSignal]:
    if not shutil.which("oasdiff"):
        return []
    try:
        result = subprocess.run(
            ["oasdiff", "breaking", str(prev), str(latest), "-f", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode not in (0, 1) or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    signals: list[RawSignal] = []
    for item in payload if isinstance(payload, list) else []:
        text = str(item.get("text") or item.get("id") or item)
        signals.append(
            RawSignal(
                vendor="openai",
                change_type=ChangeType.API_BREAKING,
                severity=Severity.CRITICAL,
                affected_pattern=text[:120],
                description=text,
                source_url=OPENAPI_SOURCE_URL,
            )
        )
    return signals


class OpenAPIDiffWorker(Worker):
    name = "OpenAPIDiffWorker"

    def run(self, *, demo: bool = False, client_state=None) -> list[RawSignal]:
        fixture_prev = fixtures_dir() / "openapi" / "previous.yaml"
        fixture_latest = fixtures_dir() / "openapi" / "latest.yaml"

        if demo:
            pair = load_openapi_pair(demo=True)
            if not pair:
                return []
            previous, latest = pair
            signals = _diff_paths(previous, latest)
            if fixture_prev.is_file() and fixture_latest.is_file():
                oas = _run_oasdiff(fixture_prev, fixture_latest)
                if oas:
                    keys = {(s.change_type, s.affected_pattern) for s in signals}
                    for s in oas:
                        if (s.change_type, s.affected_pattern) not in keys:
                            signals.append(s)
            return signals

        return self._live_diff()

    def _live_diff(self) -> list[RawSignal]:
        """Diff committed fixture baseline vs freshly cloned latest OpenAPI."""
        fixture_prev = fixtures_dir() / "openapi" / "previous.yaml"
        if not fixture_prev.is_file():
            return []

        repo = "https://github.com/openai/openai-openapi.git"
        with tempfile.TemporaryDirectory(prefix="oasdiff-") as tmp:
            tmp_path = Path(tmp)
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", repo, str(tmp_path / "repo")],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                return []
            repo_path = tmp_path / "repo"
            candidates = list(repo_path.glob("**/openapi.y*ml")) + list(
                repo_path.glob("**/openapi.json")
            )
            if not candidates:
                return []
            latest_path = candidates[0]
            previous = _load_openapi(fixture_prev)
            latest = _load_openapi(latest_path)
            _OPENAPI_PAIR_CACHE["live"] = (previous, latest)
            signals = _diff_paths(previous, latest)
            oas = _run_oasdiff(fixture_prev, latest_path)
            if oas:
                keys = {(s.change_type, s.affected_pattern) for s in signals}
                for s in oas:
                    if (s.change_type, s.affected_pattern) not in keys:
                        signals.append(s)
            return signals
