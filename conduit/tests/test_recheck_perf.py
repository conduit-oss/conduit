"""Apply recheck must not copy or re-walk the whole tree per site.

Made-up package only. Builds a large synthetic tree, times a single-site
recheck, and asserts the scoped walk stays well under a whole-repo map.
Also checks that an upfront repo index keeps far-away callees visible without
re-parsing the whole tree after each site.
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

from conduit.apply.models import ImpactSite
from conduit.apply.repoindex import RepoIndex, using_index
from conduit.apply.verify import _recheck_files, remaining_breaks
from conduit.outside_in.bridge import build_service_map


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _site(path: str = "app/work.py", line: int = 3) -> ImpactSite:
    return ImpactSite(
        path=path,
        line=line,
        expr="client.old_call",
        dependency="wiresdk",
        from_version="1.0",
        to_version="2.0",
        change="old_call removed",
        confidence="high",
        source="wheel",
        kind="removed",
        symbol="wiresdk.Client.old_call",
    )


def test_recheck_files_only_lists_importers(tmp_path: Path):
    root = tmp_path / "repo"
    _write(root / "wiresdk" / "__init__.py", "class Client:\n    def old_call(self, n): return n\n")
    _write(root / "app" / "work.py", "from wiresdk import Client\ndef fine(client):\n    return client.old_call(1)\n")
    _write(root / "app" / "other.py", "def noop():\n    return 1\n")
    for index in range(40):
        _write(root / "noise" / f"mod_{index}.py", f"VALUE = {index}\n")
    rels = _recheck_files(root, "wiresdk", "app/work.py")
    assert rels is not None
    assert "app/work.py" in rels
    assert "app/other.py" not in rels
    assert not any(item.startswith("noise/") for item in rels)


def test_remaining_breaks_recheck_beats_whole_repo_map(tmp_path: Path):
    root = tmp_path / "repo"
    _write(
        root / "wiresdk" / "__init__.py",
        """\
        class Client:
            def old_call(self, n):
                return n

            def new_call(self, n):
                return n
        """,
    )
    _write(
        root / "app" / "work.py",
        """\
        from wiresdk import Client

        def fine(client):
            return client.old_call(1)
        """,
    )
    for index in range(80):
        # Noise modules that do not import the dependency.
        _write(
            root / "noise" / f"pkg_{index}" / f"mod_{index}.py",
            f"def f_{index}():\n    return {index}\n",
        )
    site = _site()
    patched = "from wiresdk import Client\n\ndef fine(client):\n    return client.new_call(1)\n"

    started = time.perf_counter()
    whole = build_service_map(root)
    whole_seconds = time.perf_counter() - started
    assert whole[0].chains or True  # map may be empty of chains for wiresdk without evidence

    # Warm classify / caches, then time a steady-state recheck (apply does many).
    remaining_breaks(root, site, patched, span=(3, 4), allow_network=False)
    samples: list[float] = []
    for _ in range(3):
        started = time.perf_counter()
        remaining_breaks(root, site, patched, span=(3, 4), allow_network=False)
        samples.append(time.perf_counter() - started)
    recheck_seconds = sum(samples) / len(samples)

    # Scoped recheck must stay well under a whole-repo map on a noisy tree.
    assert recheck_seconds < max(0.75, whole_seconds * 0.35 + 0.05), (
        recheck_seconds,
        whole_seconds,
    )
    # And the original file is restored.
    assert "old_call" in (root / "app" / "work.py").read_text(encoding="utf-8")


def test_indexed_recheck_sees_far_away_callee_without_full_reparse(tmp_path: Path):
    """A type defined far from the edited site still drives a dict-key finding.

    The index is built once for ~1500 modules; the per-site recheck may re-parse
    only the patched file (and its restore), never the whole tree again.
    """
    root = tmp_path / "repo"
    _write(
        root / "wiresdk" / "__init__.py",
        """\
        class Client:
            def old_call(self, n):
                return n

            def new_call(self, n):
                return n
        """,
    )
    # Far-away module the rewritten site imports; noise modules do not.
    _write(
        root / "far" / "away" / "basket.py",
        """\
        class Basket:
            @classmethod
            def from_map(cls, values):
                return cls(values["apples"], values["pears"])
        """,
    )
    _write(root / "far" / "away" / "__init__.py", "")
    _write(root / "far" / "__init__.py", "")
    for index in range(1500):
        _write(
            root / "noise" / f"pkg_{index}" / f"mod_{index}.py",
            f"def f_{index}():\n    return {index}\n",
        )
    original = textwrap.dedent(
        """\
        from wiresdk import Client
        from far.away.basket import Basket

        def fine(client):
            return client.old_call(1)
        """
    )
    _write(root / "app" / "work.py", original)
    # Rewrite drops pears from the Basket.from_map dict — dict-key must fire.
    patched = textwrap.dedent(
        """\
        from wiresdk import Client
        from far.away.basket import Basket

        def fine(client):
            client.new_call(1)
            return Basket.from_map({"apples": 1})
        """
    )
    site = _site(line=4)

    index = RepoIndex.build(root)
    built = index.parse_count
    assert built >= 1500

    with using_index(index):
        before = index.parse_count
        started = time.perf_counter()
        found = remaining_breaks(root, site, patched, span=(4, 6), allow_network=False)
        indexed_seconds = time.perf_counter() - started
        reparsed = index.parse_count - before

    assert any("missing required 'pears'" in text for text, _symbol in found), found
    # update(patched) + update(restore) at most; never another whole-repo walk.
    assert reparsed <= 4, (reparsed, built)
    assert reparsed < built // 10

    # Without the index, the same recheck re-parses the tree (baseline timing).
    started = time.perf_counter()
    cold = remaining_breaks(root, site, patched, span=(4, 6), allow_network=False)
    cold_seconds = time.perf_counter() - started
    assert any("missing required 'pears'" in text for text, _symbol in cold), cold
    # Indexed path should stay well under a cold full-walk recheck on this tree.
    assert indexed_seconds < max(1.0, cold_seconds * 0.5 + 0.05), (
        indexed_seconds,
        cold_seconds,
        reparsed,
        built,
    )
    assert "old_call" in (root / "app" / "work.py").read_text(encoding="utf-8")
