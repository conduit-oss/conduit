"""CI check: mid-size fixture analysis stays under a memory ceiling."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.library_types import _package_files


def _rss_mb() -> float:
    with open(f"/proc/{os.getpid()}/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    return 0.0


def test_package_files_stay_inside_top(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "wanted").mkdir(parents=True)
    (site / "wanted" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (site / "wanted" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    other = site / "other_pkg"
    other.mkdir()
    (other / "__init__.py").write_text("y = 2\n", encoding="utf-8")
    for index in range(50):
        (other / f"bulk_{index}.py").write_text(f"v = {index}\n", encoding="utf-8")
    found = _package_files(site, "wanted")
    assert len(found) == 2
    assert all("other_pkg" not in str(path) for path in found)


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="Linux RSS only")
def test_midsize_repo_analysis_memory_ceiling(tmp_path: Path) -> None:
    """A synthetic mid-size tree must not grow without bound via library ASTs."""
    root = tmp_path / "repo"
    pkg = root / "app"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # Enough modules to stress the scanner without being a full product monorepo.
    for index in range(80):
        (pkg / f"mod_{index}.py").write_text(
            "\n".join(
                [
                    "import json",
                    "import os",
                    "from pathlib import Path",
                    "",
                    f"def work_{index}(path: str) -> dict:",
                    "    data = json.loads(Path(path).read_text())",
                    "    return {\"ok\": True, \"n\": len(data), \"cwd\": os.getcwd()}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    before = _rss_mb()
    service_map = analyze_repo(root)
    after = _rss_mb()
    growth = after - before
    assert service_map is not None
    # Scoped library parsing + AST release should keep growth well under 400MB
    # on this fixture. The previous site-packages walk blew past several GB.
    assert growth < 400.0, f"RSS grew {growth:.1f}MB (HWM before={before:.1f} after={after:.1f})"
