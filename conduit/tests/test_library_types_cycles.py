"""Cycle-safe library type walks and walk budgets.

Uses only invented packages (``acmekit`` and friends). No external SDKs.
"""

from __future__ import annotations

import sys
import textwrap
import zipfile
from pathlib import Path

from conduit.outside_in import library_types as lt
from conduit.outside_in.graph import analyze_python
from conduit.outside_in.library_types import LibraryTypes
from conduit.outside_in.specs import ServiceSpec


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _acmekit(root: Path) -> Path:
    """Tiny package whose types reference each other cyclically."""
    lib = root / "lib"
    _write(
        lib / "acmekit" / "__init__.py",
        """
        from __future__ import annotations

        from typing import List

        # Self-referential alias — walking it must not recurse forever.
        Node = List["Node"]

        class A:
            peer: "B"

            def child(self) -> "B":
                return B()

            def ping(self) -> str:
                return "a"

        class B:
            kids: List["A"]

            def parent(self) -> "A":
                return A()

            def pong(self) -> str:
                return "b"
        """,
    )
    return lib


def _spec() -> ServiceSpec:
    return ServiceSpec(
        service_id="acmekit",
        package="acmekit",
        modules=("acmekit",),
        kind="api",
    )


def test_cyclic_acmekit_types_still_report_call_sites(tmp_path: Path) -> None:
    """Impact-style mapping completes on cyclic types and keeps the call sites."""
    lib = _acmekit(tmp_path)
    _write(
        tmp_path / "app" / "use.py",
        """
        from acmekit import A

        def run(item: A):
            return item.child().pong()

        def bounce(item: A):
            return item.child().parent().ping()
        """,
    )
    sys.path.insert(0, str(lib))
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        types = LibraryTypes(None)
        assert types.call_return("acmekit.A.child") == ("acmekit.B",)
        assert types.instance_attribute("acmekit.A", "peer") == "acmekit.B"
        assert types.instance_attribute("acmekit.B", "kids") == "acmekit.A"
        assert types.call_return("acmekit.B.parent") == ("acmekit.A",)

        mapped = analyze_python(tmp_path / "app", (_spec(),))
    finally:
        sys.setrecursionlimit(previous)
        sys.path.remove(str(lib))

    methods = {
        chain.sdk_method
        for chain in mapped.chains
        if not chain.via_wrapper
    }
    assert any(method.endswith("B.pong") for method in methods)
    assert any(method.endswith("A.ping") for method in methods)


def test_walk_budget_stops_a_deep_define_chain(tmp_path: Path, monkeypatch) -> None:
    """A deep inheritance chain comes back unknown instead of RecursionError."""
    pkg = tmp_path / "deepkit"
    pkg.mkdir()
    lines = ["class C0:\n    def make(self):\n        return 1\n"]
    for index in range(1, 80):
        lines.append(f"class C{index}(C{index - 1}):\n    pass\n")
    (pkg / "__init__.py").write_text("".join(lines), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(lt, "_MAX_DEFINE_DEPTH", 8)
    monkeypatch.setattr(lt, "_MAX_WALK_NODES", 32)

    types = LibraryTypes(None)
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(120)
    try:
        found = types.call_return("deepkit.C79.make")
    finally:
        sys.setrecursionlimit(previous)

    assert isinstance(found, tuple)
    # Budget / depth cap: unknown rather than a crash.
    assert found == ()


def test_walk_budget_stops_a_wide_fact_extract(tmp_path: Path, monkeypatch) -> None:
    """A wide public surface stops once the node budget is spent."""
    pkg = tmp_path / "widekit"
    pkg.mkdir()
    chunks = ["class Hub:\n    pass\n"]
    for index in range(40):
        chunks.append(
            f"class C{index}:\n"
            f"    link: \"C{(index + 1) % 40}\"\n"
            f"    def go(self) -> \"C{(index + 1) % 40}\":\n"
            f"        return C{(index + 1) % 40}()\n"
        )
    (pkg / "__init__.py").write_text("".join(chunks), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(lt, "_MAX_WALK_NODES", 25)

    types = LibraryTypes(None)
    mods = types._modules("widekit")
    assert mods
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        facts = types._extract_facts("widekit", mods)
    finally:
        sys.setrecursionlimit(previous)

    assert isinstance(facts.returns, dict)
    # With a tiny budget the extract must stop early, not walk every symbol.
    assert len(facts.returns) < 40


def test_fact_extract_does_not_recurse_when_the_cache_is_full(
    tmp_path: Path, monkeypatch
) -> None:
    """Evicting the distribution currently being extracted must not re-enter it."""
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
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        found = types.call_return("cyclebox0.Box.make")
    finally:
        sys.setrecursionlimit(previous)
    assert isinstance(found, tuple)


def test_installed_root_swallows_find_spec_failures(monkeypatch) -> None:
    """A broken install must not crash impact when resolving a package root."""

    def boom(_name: str):
        raise ModuleNotFoundError("No module named 'missing_dep'")

    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    assert lt._installed_root("acmekit") is None
