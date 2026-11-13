"""Libtypes must not unpack classify sdists or oversized wheels."""

from __future__ import annotations

import zipfile
from pathlib import Path

from conduit.outside_in import library_types as lt
from conduit.outside_in.library_types import LibraryTypes


def test_unpack_and_pick_url_reject_sdist_and_oversized_wheel(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(lt, "_MAX_DOWNLOAD", 64)
    monkeypatch.setattr(lt, "_libtypes_cache", lambda: tmp_path / "cache")

    sdist = tmp_path / "demo-1.0.tar.gz"
    sdist.write_bytes(b"sdist-body")
    assert lt._unpack(sdist, "demo") is None

    oversized = tmp_path / "demo-1.0-py3-none-any.whl"
    oversized.write_bytes(b"x" * 128)
    assert lt._unpack(oversized, "demo") is None

    assert (
        lt._pick_url(
            [
                {
                    "filename": "demo-1.0.tar.gz",
                    "url": "https://example.test/demo-1.0.tar.gz",
                    "size": 10,
                    "packagetype": "sdist",
                }
            ]
        )
        is None
    )

    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir()
    (wheels_dir / "demo-1.0.tar.gz").write_bytes(b"sdist")
    (wheels_dir / "demo-1.0-py3-none-any.whl").write_bytes(b"y" * 128)
    monkeypatch.setattr(lt, "_wheel_dirs", lambda: (wheels_dir,))
    assert lt._cached_wheel("demo", "1.0") is None


def test_fact_extract_does_not_recurse_when_the_cache_is_full(
    tmp_path: Path, monkeypatch
) -> None:
    """Evicting the distribution currently being extracted must not re-enter it.

    A cycle of small wheels plus a tiny module cache used to drop the in-progress
    AST and parse it again until RecursionError.
    """
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    count = 4
    for index in range(count):
        nxt = f"cyclebox{(index + 1) % count}"
        name = f"cyclebox{index}"
        source = (
            f"import {nxt}\n"
            "\n"
            "class Box:\n"
            f"    def make(self) -> {nxt}.Box:\n"
            f"        return {nxt}.Box()\n"
        )
        path = wheels / f"{name}-1.0.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{name}/__init__.py", source)

    monkeypatch.setenv("CONDUIT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(lt, "_MAX_CACHED_DISTS", 2)
    monkeypatch.setattr(lt, "_wheel_dirs", lambda: (wheels,))
    monkeypatch.setattr(lt, "_installed_root", lambda _top: None)

    types = LibraryTypes(None)
    found = types.call_return("cyclebox0.Box.make")
    assert isinstance(found, tuple)


def test_define_chain_stops_before_the_stack_overflows(tmp_path: Path, monkeypatch) -> None:
    """A long base chain must come back unknown, not raise RecursionError."""
    import sys

    pkg = tmp_path / "chainbox"
    pkg.mkdir()
    lines = ["class C0:\n    def make(self):\n        return 1\n"]
    for index in range(1, 80):
        lines.append(f"class C{index}(C{index - 1}):\n    pass\n")
    (pkg / "__init__.py").write_text("".join(lines), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(lt, "_MAX_DEFINE_DEPTH", 8)
    types = LibraryTypes(None)
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(120)
    try:
        found = types.call_return("chainbox.C79.make")
    finally:
        sys.setrecursionlimit(previous)
    assert isinstance(found, tuple)
