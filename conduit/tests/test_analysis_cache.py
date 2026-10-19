"""On-disk analysis cache: hits, corruption, and source-digest coverage."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conduit.analysis_cache import (
    ANALYSIS_SOURCE_FILES,
    ANALYSIS_VERSION,
    analysis_source_digest,
    cache_root,
    cache_version,
    entry_path,
    file_sha256,
    package_dir,
    read_json,
    without_cache,
    write_json,
)
from conduit.impact.api import Symbol, diff_wheels, load_api
from conduit.outside_in.library_types import LibraryTypes

STUBS = Path(__file__).resolve().parents[1] / "tests" / "stubs"
OLD = STUBS / "acme-sdk" / "wheels" / "acme_sdk-1.0.0-py3-none-any.whl"
NEW = STUBS / "acme-sdk" / "wheels" / "acme_sdk-1.2.0-py3-none-any.whl"


@pytest.fixture()
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("CONDUIT_NO_CACHE", raising=False)
    monkeypatch.setenv("CONDUIT_CACHE_DIR", str(tmp_path / "conduit-cache"))
    analysis_source_digest.cache_clear()
    return tmp_path / "conduit-cache"


def test_analysis_source_files_exist_and_digest_is_stable():
    """Cache keys include a digest of analysis modules — no manual bump required.

    ANALYSIS_VERSION remains a manual epoch for intentional invalidation.
    Changing any digested file changes ``cache_version()`` automatically, so
    stale entries are never reused. This test fails if a listed source path
    is removed without updating ANALYSIS_SOURCE_FILES.
    """
    root = package_dir()
    missing = [rel for rel in ANALYSIS_SOURCE_FILES if not (root / rel).is_file()]
    assert not missing, f"analysis source missing: {missing}"
    assert ANALYSIS_VERSION
    first = analysis_source_digest()
    analysis_source_digest.cache_clear()
    assert analysis_source_digest() == first
    assert cache_version().startswith(f"{ANALYSIS_VERSION}-")


def test_cache_root_respects_xdg_and_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CONDUIT_NO_CACHE", raising=False)
    monkeypatch.delenv("CONDUIT_CACHE_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    assert cache_root() == xdg / "conduit"
    override = tmp_path / "override"
    monkeypatch.setenv("CONDUIT_CACHE_DIR", str(override))
    assert cache_root() == override


def test_load_api_cache_hit_matches_cold(cache_home: Path):
    cold = load_api(OLD, include_private=True)
    assert cold
    warm = load_api(OLD, include_private=True)
    assert warm == cold
    key = f"{file_sha256(OLD)}-all"
    assert entry_path("api", key).is_file()


def test_diff_wheels_cache_hit_matches_cold(cache_home: Path):
    cold = diff_wheels(OLD, NEW)
    warm = diff_wheels(OLD, NEW)
    assert list(cold) == list(warm)
    assert cold.return_classes == warm.return_classes
    assert cold.methods == warm.methods
    assert cold.surviving == warm.surviving


def test_corrupted_cache_entry_is_ignored(cache_home: Path):
    cold = load_api(OLD, include_private=True)
    key = f"{file_sha256(OLD)}-all"
    path = entry_path("api", key)
    path.write_text("{not-json", encoding="utf-8")
    again = load_api(OLD, include_private=True)
    assert again == cold


def test_old_analysis_version_cache_entry_is_ignored(cache_home: Path):
    cold = load_api(OLD, include_private=True)
    key = f"{file_sha256(OLD)}-all"
    path = entry_path("api", key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["analysis_version"] = "0-deadbeefdeadbeef"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_json("api", key) is None
    again = load_api(OLD, include_private=True)
    assert again == cold


def test_no_cache_flag_skips_disk(cache_home: Path):
    with without_cache():
        load_api(OLD, include_private=True)
    assert not any(cache_home.rglob("*.json"))


def test_libtypes_facts_cache_hit(cache_home: Path, tmp_path: Path):
    wheels = cache_home / "wheels"
    wheels.mkdir(parents=True)
    dest = wheels / OLD.name
    dest.write_bytes(OLD.read_bytes())
    root = tmp_path / "repo"
    root.mkdir()
    (root / "requirements.txt").write_text("acme-sdk==1.0.0\n", encoding="utf-8")
    first = LibraryTypes(root)
    first._modules("acme_sdk")
    assert "acme_sdk" in first._facts
    digest = file_sha256(dest)
    payload = read_json("libtypes", digest)
    assert payload is not None
    second = LibraryTypes(root)
    second._modules("acme_sdk")
    assert "acme_sdk" in second._facts
    assert second._facts["acme_sdk"].to_json() == payload


def test_symbol_roundtrip_through_api_cache(cache_home: Path):
    api = load_api(OLD, include_private=True)
    assert all(isinstance(sym, Symbol) for sym in api.values())
    warm = load_api(OLD, include_private=True)
    for name, sym in api.items():
        assert warm[name] == sym
