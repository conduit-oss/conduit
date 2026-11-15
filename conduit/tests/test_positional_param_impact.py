"""Positional args and omitted required params must hit impact's matcher.

Made-up package only (acmekit). Drives the call-site matcher with wheel fixtures;
does not edit existing expectations elsewhere.
"""

from __future__ import annotations

import json
import textwrap
import zipfile
from pathlib import Path

from conduit.impact.api import diff_wheels
from conduit.impact.run import collect_impacts
from conduit.outside_in import evidence as evidence_mod
from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.evidence import classify_distribution

DIST = "acmekit"


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _clear_classify_cache() -> None:
    evidence_mod._MEMORY.clear()
    cache = evidence_mod.cache_dir()
    if not cache.is_dir():
        return
    for path in cache.glob("*acmekit*"):
        path.unlink(missing_ok=True)


def _wheel(path: Path, version: str, source: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{DIST}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{DIST}-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{DIST}-{version}.dist-info/top_level.txt", f"{DIST}\n")
        archive.writestr(f"{DIST}/__init__.py", textwrap.dedent(source).lstrip("\n"))


# Public methods (ping/remove) make Store/Queue product classes so socket I/O
# classifies acmekit as an outside service — same shape as coverage_grid's zzgrid.
_V1 = '''
import socket

class Store:
    host = "api.acmekit.test"

    def __init__(self, name):
        self.name = name
        try:
            self._sock = socket.create_connection((self.host, 443), timeout=0.001)
        except OSError:
            self._sock = None

    def ping(self):
        return self.name

class Queue:
    def remove(self, name, value, num=0):
        return (name, value, num)

def keep(a, b):
    return a, b

def only_name(name):
    return name
'''

_V2 = '''
import socket

class Store:
    host = "api.acmekit.test"

    def __init__(self, api_key, host):
        self.api_key = api_key
        self.host = host
        try:
            self._sock = socket.create_connection((self.host, 443), timeout=0.001)
        except OSError:
            self._sock = None

    def ping(self):
        return self.api_key

class Queue:
    def remove(self, name, count, value):
        return (name, count, value)

def keep(a, b):
    return a, b

def only_name(name, region=None):
    return name, region
'''

_V2_HOST_ONLY = '''
import socket

class Store:
    host = "api.acmekit.test"

    def __init__(self, api_key, host):
        self.api_key = api_key
        self.host = host
        try:
            self._sock = socket.create_connection((self.host, 443), timeout=0.001)
        except OSError:
            self._sock = None

    def ping(self):
        return self.api_key
'''

_V1_HOST_BASE = '''
import socket

class Store:
    host = "api.acmekit.test"

    def __init__(self, api_key):
        self.api_key = api_key
        try:
            self._sock = socket.create_connection((self.host, 443), timeout=0.001)
        except OSError:
            self._sock = None

    def ping(self):
        return self.api_key
'''


def _fixtures(tmp_path: Path, *, old_src: str = _V1, new_src: str = _V2) -> Path:
    fixtures = tmp_path / "fixtures"
    wheels = fixtures / "wheels"
    wheels.mkdir(parents=True)
    old = wheels / f"{DIST}-1.0.0-py3-none-any.whl"
    new = wheels / f"{DIST}-2.0.0-py3-none-any.whl"
    _wheel(old, "1.0.0", old_src)
    _wheel(new, "2.0.0", new_src)
    (fixtures / "index.json").write_text(
        json.dumps(
            {
                "packages": {
                    DIST: {
                        "from": "1.0.0",
                        "to": "2.0.0",
                        "wheels": {
                            "1.0.0": f"wheels/{old.name}",
                            "2.0.0": f"wheels/{new.name}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return fixtures


def _install_v1(repo: Path, source: str = _V1) -> None:
    site = repo / ".venv" / "lib" / "python3.12" / "site-packages"
    info = site / f"{DIST}-1.0.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Name: {DIST}\nVersion: 1.0.0\n", encoding="utf-8")
    (info / "top_level.txt").write_text(f"{DIST}\n", encoding="utf-8")
    pkg = site / DIST
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    (repo / "requirements.txt").write_text(f"{DIST}==1.0.0\n", encoding="utf-8")


def _impacts(tmp_path: Path, app_source: str, *, old_src: str = _V1, new_src: str = _V2):
    _clear_classify_cache()
    fixtures = _fixtures(tmp_path, old_src=old_src, new_src=new_src)
    repo = tmp_path / "repo"
    repo.mkdir()
    _install_v1(repo, old_src)
    _write(repo / "app.py", app_source)
    found = classify_distribution(DIST, "1.0.0", root=repo)
    assert found.service is True, found
    service_map = analyze_repo(repo)
    impacts, _diffed, notes = collect_impacts(
        repo,
        service_map,
        fixtures=fixtures,
        dep=DIST,
        from_version="1.0.0",
        to_version="2.0.0",
        allow_network=False,
    )
    assert notes == []
    return impacts


def _site_lines(impacts, *, kinds: set[str] | None = None) -> set[int]:
    lines: set[int] = set()
    for item in impacts:
        if kinds is not None and item.kind not in kinds:
            continue
        for site in item.sites:
            if site.via_wrapper:
                continue
            if site.path.endswith("app.py"):
                lines.add(site.line)
    return lines


def _line_for(source: str, needle: str) -> int:
    for index, line in enumerate(textwrap.dedent(source).lstrip("\n").splitlines(), 1):
        if needle in line:
            return index
    raise AssertionError(f"missing {needle!r} in source")


def test_positional_renamed_param_is_flagged(tmp_path: Path):
    source = """
        import acmekit

        def run():
            return acmekit.Store("orders")
        """
    impacts = _impacts(tmp_path, source)
    line = _line_for(source, 'Store("orders")')
    assert line in _site_lines(impacts, kinds={"param_renamed", "param_removed", "param_added"})


def test_positional_reordered_params_are_flagged(tmp_path: Path):
    source = """
        import acmekit

        def run(item):
            q = acmekit.Queue()
            return q.remove("jobs", item, 0)
        """
    impacts = _impacts(tmp_path, source)
    line = _line_for(source, 'q.remove("jobs", item, 0)')
    assert line in _site_lines(impacts, kinds={"param_renamed", "param_removed", "param_added"})


def test_constructor_omitting_new_required_param_is_flagged(tmp_path: Path):
    source = """
        import acmekit

        def run():
            return acmekit.Store("orders")
        """
    impacts = _impacts(tmp_path, source)
    line = _line_for(source, 'Store("orders")')
    assert line in _site_lines(impacts, kinds={"param_added"})


def test_keyword_renamed_param_still_flagged(tmp_path: Path):
    source = """
        import acmekit

        def run():
            return acmekit.Store(name="orders")
        """
    impacts = _impacts(tmp_path, source)
    line = _line_for(source, 'Store(name="orders")')
    assert line in _site_lines(impacts, kinds={"param_renamed", "param_removed"})


def test_keyword_reordered_param_still_flagged(tmp_path: Path):
    source = """
        import acmekit

        def run(item):
            q = acmekit.Queue()
            return q.remove("jobs", item, num=0)
        """
    impacts = _impacts(tmp_path, source)
    line = _line_for(source, "num=0")
    assert line in _site_lines(impacts, kinds={"param_renamed", "param_removed"})


def test_unaffected_positional_binding_is_not_flagged(tmp_path: Path):
    source = """
        import acmekit

        def run():
            return acmekit.keep(1, 2)
        """
    impacts = _impacts(tmp_path, source)
    line = _line_for(source, "keep(1, 2)")
    assert line not in _site_lines(impacts)


def test_optional_param_omitted_is_not_flagged(tmp_path: Path):
    source = """
        import acmekit

        def run():
            return acmekit.only_name("x")
        """
    impacts = _impacts(tmp_path, source)
    line = _line_for(source, 'only_name("x")')
    assert line not in _site_lines(impacts, kinds={"param_added"})


def test_new_required_param_already_passed_by_keyword_not_flagged(tmp_path: Path):
    source = """
        import acmekit

        def run():
            return acmekit.Store(api_key="k", host="h")
        """
    impacts = _impacts(
        tmp_path,
        source,
        old_src=_V1_HOST_BASE,
        new_src=_V2_HOST_ONLY,
    )
    line = _line_for(source, 'Store(api_key="k", host="h")')
    assert line not in _site_lines(impacts, kinds={"param_added"})


def test_new_required_param_already_passed_by_position_not_flagged(tmp_path: Path):
    source = """
        import acmekit

        def run():
            return acmekit.Store("k", "h")
        """
    impacts = _impacts(
        tmp_path,
        source,
        old_src=_V1_HOST_BASE,
        new_src=_V2_HOST_ONLY,
    )
    line = _line_for(source, 'Store("k", "h")')
    assert line not in _site_lines(impacts, kinds={"param_added"})


def test_star_args_call_unchanged_when_unbound(tmp_path: Path):
    source = """
        import acmekit

        def run(args):
            return acmekit.Store(*args)
        """
    impacts = _impacts(tmp_path, source)
    line = _line_for(source, "Store(*args)")
    # Today: positional rename / required-host omission are not visible through *args.
    assert line not in _site_lines(impacts, kinds={"param_renamed", "param_removed", "param_added"})


def test_kwargs_call_unchanged_when_unbound(tmp_path: Path):
    source = """
        import acmekit

        def run(kwargs):
            return acmekit.Store(**kwargs)
        """
    impacts = _impacts(tmp_path, source)
    line = _line_for(source, "Store(**kwargs)")
    assert line not in _site_lines(impacts, kinds={"param_renamed", "param_removed", "param_added"})


def test_wheel_diff_records_store_rename_and_required_host(tmp_path: Path):
    fixtures = _fixtures(tmp_path)
    old = fixtures / "wheels" / f"{DIST}-1.0.0-py3-none-any.whl"
    new = fixtures / "wheels" / f"{DIST}-2.0.0-py3-none-any.whl"
    changes = diff_wheels(old, new)
    assert any(
        item.kind == "param_renamed" and item.old == "name" and item.new == "api_key"
        for item in changes
    )
    assert any(item.kind == "param_added" and item.new == "host" for item in changes)
    assert getattr(changes, "before_posparams", {}).get("acmekit.Store") == ("name",)
    assert getattr(changes, "after_posparams", {}).get("acmekit.Store") == ("api_key", "host")
