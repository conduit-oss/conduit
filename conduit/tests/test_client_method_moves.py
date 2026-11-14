"""Module-level calls move onto a client instance when the target wheel says so.

Made-up packages only (acmekit). No real SDK, product, or migration-table names.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.names import (
    RemovedModuleImports,
    _static_defining_node,
    relocate_removed_imports,
)
from conduit.apply.signatures import _modules

from test_apply import _settings


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _client_wheels(tmp_path: Path) -> Path:
    """acmekit 1.0: module-level init/list_things/Thing. 2.0: Client methods."""
    old = tmp_path / "fixtures" / "old"
    new = tmp_path / "fixtures" / "new"
    _write(
        old / "acmekit" / "__init__.py",
        """\
        def init(api_key=None, region=None):
            return None

        def list_things():
            return []

        def create_thing(name):
            return name

        class Thing:
            def __init__(self, name):
                self.name = name
        """,
    )
    _write(
        new / "acmekit" / "__init__.py",
        """\
        from .client import Client
        """,
    )
    _write(
        new / "acmekit" / "client.py",
        """\
        class Client:
            def __init__(self, api_key=None, region=None):
                self.api_key = api_key
                self.region = region

            def list_things(self):
                return []

            def create_thing(self, name):
                return name

            def Thing(self, name):
                return name
        """,
    )
    fixtures = tmp_path / "fixtures"
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"acmekit": {"wheels": {"1.0": "old", "2.0": "new"}}}}),
        encoding="utf-8",
    )
    return fixtures


def _maps(tmp_path: Path) -> list[RemovedModuleImports]:
    fixtures = _client_wheels(tmp_path)
    return [
        RemovedModuleImports(
            package="acmekit",
            version="2.0",
            modules=_modules(str(fixtures / "new")),
            wheel=fixtures / "new",
            old_modules=_modules(str(fixtures / "old")),
            old_wheel=fixtures / "old",
        )
    ]


def test_module_function_rewrites_onto_client_once(tmp_path: Path):
    """Removed module functions become client methods; one Client() is inserted."""
    maps = _maps(tmp_path)
    source = (
        "import acmekit\n"
        "\n"
        "def use():\n"
        "    acmekit.init(api_key='k', region='us')\n"
        "    names = acmekit.list_things()\n"
        "    acmekit.create_thing('a')\n"
        "    return acmekit.Thing('b'), names\n"
    )
    updated, hits = relocate_removed_imports(source, "use.py", maps)
    assert "acmekit.init" not in updated
    assert "acmekit.list_things" not in updated
    assert "acmekit.create_thing" not in updated
    assert "acmekit.Thing" not in updated
    assert "from acmekit import Client" in updated
    assert updated.count("Client(") == 1
    assert "list_things()" in updated
    assert "create_thing('a')" in updated
    assert any(hit.moved for hit in hits)
    assert all(not hit.missing for hit in hits if hit.moved)


def test_reuses_existing_client_instance(tmp_path: Path):
    """An existing Client(...) is reused; no second construction is inserted."""
    maps = _maps(tmp_path)
    source = (
        "import acmekit\n"
        "from acmekit import Client\n"
        "\n"
        "client = Client(api_key='already')\n"
        "\n"
        "def use():\n"
        "    return acmekit.list_things(), acmekit.create_thing('x')\n"
    )
    updated, hits = relocate_removed_imports(source, "use.py", maps)
    assert updated.count("Client(") == 1
    assert "client = Client(api_key='already')" in updated
    assert "client.list_things()" in updated
    assert "client.create_thing('x')" in updated
    assert "acmekit.list_things" not in updated
    assert any(hit.moved for hit in hits)


def test_subclass_of_removed_class_is_needs_human(tmp_path: Path):
    """Subclassing a removed class is not rewritten into an invented base."""
    fixtures = _client_wheels(tmp_path)
    # Extend old wheel with a class that disappears; new wheel has no replacement base.
    old = fixtures / "old"
    _write(
        old / "acmekit" / "__init__.py",
        """\
        def init(api_key=None):
            return None

        def list_things():
            return []

        class Thing:
            def __init__(self, name):
                self.name = name

        class LegacyModel:
            def run(self):
                return 1
        """,
    )
    root = tmp_path / "app"
    _write(
        root / "use.py",
        """\
        import acmekit

        class Mine(acmekit.LegacyModel):
            def run(self):
                return 2
        """,
    )
    report = apply_sites(
        sites_from_payload(
            {
                "dependency": "acmekit",
                "impacts": [
                    {
                        "dependency": "acmekit",
                        "from_version": "1.0",
                        "to_version": "2.0",
                        "change": "LegacyModel removed",
                        "confidence": "high",
                        "source": "wheel",
                        "kind": "removed",
                        "symbol": "acmekit.LegacyModel",
                        "sites": [
                            {
                                "path": "use.py",
                                "line": 3,
                                "expr": "acmekit.LegacyModel",
                                "confidence": "high",
                                "via_wrapper": False,
                            }
                        ],
                    }
                ],
                "deprecations": [],
                "diffed": ["acmekit"],
                "notes": [],
            }
        ),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            client=FakeModelClient(responses=[]),
            fixtures=fixtures,
        ),
    )
    text = (root / "use.py").read_text(encoding="utf-8")
    assert "class Mine(acmekit.LegacyModel)" in text
    assert "class Mine(Client)" not in text
    (result,) = report.results
    assert result.status == SiteStatus.NEEDS_HUMAN
    assert result.status != SiteStatus.FIXED_VERIFIED


def test_no_rewrite_when_no_matching_client_method(tmp_path: Path):
    """A removed module function with no client method counterpart is not rewritten."""
    fixtures = _client_wheels(tmp_path)
    old = fixtures / "old"
    _write(
        old / "acmekit" / "__init__.py",
        """\
        def init(api_key=None):
            return None

        def list_things():
            return []

        def orphan_only():
            return 1
        """,
    )
    maps = [
        RemovedModuleImports(
            package="acmekit",
            version="2.0",
            modules=_modules(str(fixtures / "new")),
            wheel=fixtures / "new",
            old_modules=_modules(str(old)),
            old_wheel=old,
        )
    ]
    source = "import acmekit\n\ndef use():\n    return acmekit.orphan_only()\n"
    updated, hits = relocate_removed_imports(source, "use.py", maps)
    assert updated == source
    assert any(hit.missing for hit in hits)
    assert all(not hit.moved for hit in hits)


def test_apply_rewrites_module_calls_onto_client_without_model(tmp_path: Path):
    """Full apply path: module calls move onto Client with no model involvement."""
    fixtures = _client_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "use.py",
        """\
        import acmekit

        def use():
            acmekit.init(api_key='k')
            return acmekit.list_things(), acmekit.create_thing('z')
        """,
    )
    report = apply_sites(
        sites_from_payload(
            {
                "dependency": "acmekit",
                "impacts": [
                    {
                        "dependency": "acmekit",
                        "from_version": "1.0",
                        "to_version": "2.0",
                        "change": "list_things moved onto Client",
                        "confidence": "high",
                        "source": "wheel",
                        "kind": "removed",
                        "symbol": "acmekit.list_things",
                        "sites": [
                            {
                                "path": "use.py",
                                "line": 5,
                                "expr": "acmekit.list_things",
                                "confidence": "high",
                                "via_wrapper": False,
                            },
                            {
                                "path": "use.py",
                                "line": 5,
                                "expr": "acmekit.create_thing",
                                "confidence": "high",
                                "via_wrapper": False,
                            },
                            {
                                "path": "use.py",
                                "line": 4,
                                "expr": "acmekit.init",
                                "confidence": "high",
                                "via_wrapper": False,
                            },
                        ],
                    }
                ],
                "deprecations": [],
                "diffed": ["acmekit"],
                "notes": [],
            }
        ),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            client=FakeModelClient(responses=[]),
            fixtures=fixtures,
        ),
    )
    text = (root / "use.py").read_text(encoding="utf-8")
    assert "acmekit.list_things" not in text
    assert "acmekit.init" not in text
    assert "from acmekit import Client" in text
    assert text.count("Client(") == 1
    assert "api_key='k'" in text or 'api_key="k"' in text or "api_key='k'" in text
    assert all(item.status != SiteStatus.FIXED_VERIFIED or True for item in report.results)
    assert any(
        item.status in {SiteStatus.FIXED_UNVERIFIED, SiteStatus.FIXED_NOT_COVERED}
        for item in report.results
    )
    assert not any(item.status == SiteStatus.FIXED_VERIFIED for item in report.results)


def _cyclic_reexport_wheels(tmp_path: Path) -> Path:
    """Target wheel re-exports in a cycle (A↔B) and self-re-exports at the root."""
    old = tmp_path / "fixtures" / "old"
    new = tmp_path / "fixtures" / "new"
    _write(
        old / "acmekit" / "__init__.py",
        """\
        def list_things():
            return []
        """,
    )
    _write(
        new / "acmekit" / "__init__.py",
        """\
        from .a import Client, shared
        from acmekit import shared as also_shared
        """,
    )
    _write(
        new / "acmekit" / "a.py",
        """\
        from acmekit.b import Client, shared
        """,
    )
    _write(
        new / "acmekit" / "b.py",
        """\
        from acmekit.a import Client, shared
        """,
    )
    fixtures = tmp_path / "fixtures"
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"acmekit": {"wheels": {"1.0": "old", "2.0": "new"}}}}),
        encoding="utf-8",
    )
    return fixtures


def test_cyclic_reexports_do_not_crash_apply(tmp_path: Path):
    """A↔B re-exports and a self re-export must not RecursionError during apply."""
    fixtures = _cyclic_reexport_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "use.py",
        """\
        import acmekit

        def use():
            return acmekit.list_things()
        """,
    )
    # Must finish: cycle-safe resolve + client-relocate errors fall through.
    report = apply_sites(
        sites_from_payload(
            {
                "dependency": "acmekit",
                "impacts": [
                    {
                        "dependency": "acmekit",
                        "from_version": "1.0",
                        "to_version": "2.0",
                        "change": "list_things removed",
                        "confidence": "high",
                        "source": "wheel",
                        "kind": "removed",
                        "symbol": "acmekit.list_things",
                        "sites": [
                            {
                                "path": "use.py",
                                "line": 4,
                                "expr": "acmekit.list_things",
                                "confidence": "high",
                                "via_wrapper": False,
                            }
                        ],
                    }
                ],
                "deprecations": [],
                "diffed": ["acmekit"],
                "notes": [],
            }
        ),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            client=FakeModelClient(responses=[]),
            fixtures=fixtures,
        ),
    )
    assert report.results
    text = (root / "use.py").read_text(encoding="utf-8")
    # No client to relocate onto: source falls through unchanged.
    assert "acmekit.list_things" in text
    maps = [
        RemovedModuleImports(
            package="acmekit",
            version="2.0",
            modules=_modules(str(fixtures / "new")),
            wheel=fixtures / "new",
            old_modules=_modules(str(fixtures / "old")),
            old_wheel=fixtures / "old",
        )
    ]
    updated, _hits = relocate_removed_imports(text, "use.py", maps)
    assert updated == text


def test_deep_acyclic_reexport_chain_resolves(tmp_path: Path):
    """A long acyclic re-export chain still reaches the defining binding."""
    root = tmp_path / "pkg"
    _write(
        root / "acmekit" / "__init__.py",
        """\
        from acmekit.m0 import deep_name
        """,
    )
    for i in range(12):
        nxt = i + 1
        if nxt < 12:
            body = f"from acmekit.m{nxt} import deep_name\n"
        else:
            body = "def deep_name():\n    return 1\n"
        _write(root / "acmekit" / f"m{i}.py", body)
    modules = _modules(str(root))
    node, owner = _static_defining_node("acmekit.deep_name", "acmekit", modules)
    assert node is not None
    assert getattr(node, "name", None) == "deep_name"
    assert owner == "acmekit.m11"


def _relocated_index_wheels(tmp_path: Path) -> Path:
    """acmekit 1.0: Index at package root. 2.0: Index moved to acmekit.data.

    ``query`` / ``upsert`` survive on the moved class. ``legacy_scan`` does not.
    A Client is also present so constructor sites can still move onto it.
    """
    old = tmp_path / "fixtures" / "old"
    new = tmp_path / "fixtures" / "new"
    _write(
        old / "acmekit" / "__init__.py",
        """\
        class Index:
            def __init__(self, name):
                self.name = name

            def query(self, vector, top_k=3):
                return []

            def upsert(self, vectors):
                return None

            def legacy_scan(self):
                return []

        class Client:
            def __init__(self, api_key=None):
                self.api_key = api_key
        """,
    )
    _write(
        new / "acmekit" / "__init__.py",
        """\
        from .client import Client
        """,
    )
    _write(
        new / "acmekit" / "client.py",
        """\
        class Client:
            def __init__(self, api_key=None):
                self.api_key = api_key

            def Index(self, name):
                from acmekit.data import Index
                return Index(name)
        """,
    )
    _write(
        new / "acmekit" / "data.py",
        """\
        class Index:
            def __init__(self, name, host=None):
                self.name = name
                self.host = host

            def query(self, vector, top_k=3):
                return []

            def upsert(self, vectors):
                return None
        """,
    )
    fixtures = tmp_path / "fixtures"
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"acmekit": {"wheels": {"1.0": "old", "2.0": "new"}}}}),
        encoding="utf-8",
    )
    return fixtures


def _removed_method_payload(path: str, line: int, expr: str, symbol: str) -> dict:
    return {
        "dependency": "acmekit",
        "impacts": [
            {
                "dependency": "acmekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": f"{symbol.rsplit('.', 1)[-1]} removed",
                "confidence": "high",
                "source": "wheel",
                "kind": "removed",
                "symbol": symbol,
                "sites": [
                    {
                        "path": path,
                        "line": line,
                        "expr": expr,
                        "confidence": "high",
                        "via_wrapper": False,
                    }
                ],
            }
        ],
        "deprecations": [],
        "diffed": ["acmekit"],
        "notes": [],
    }


def test_self_attr_receiver_traced_to_constructor_is_not_needs_human(tmp_path: Path):
    """``self.index = acmekit.Index(...)`` then ``self.index.query`` stays fine."""
    fixtures = _relocated_index_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "store.py",
        """\
        import acmekit

        class Store:
            def __init__(self, name):
                self.index = acmekit.Index(name)

            def search(self, vector):
                return self.index.query(vector, top_k=2)
        """,
    )
    report = apply_sites(
        sites_from_payload(
            _removed_method_payload("store.py", 8, "self.index.query", "acmekit.Index.query")
        ),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            client=FakeModelClient(responses=[]),
            fixtures=fixtures,
        ),
    )
    (result,) = report.results
    assert result.status != SiteStatus.NEEDS_HUMAN
    assert result.status == SiteStatus.SKIPPED
    assert "relocated" in result.why.lower() or "constructor" in result.why.lower()
    text = (root / "store.py").read_text(encoding="utf-8")
    assert "self.index.query" in text


def test_lazy_init_self_attr_receiver_traced_to_constructor(tmp_path: Path):
    """Constructor in a lazy-init helper still clears a later ``self.index.query``."""
    fixtures = _relocated_index_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "store.py",
        """\
        import acmekit

        class Store:
            def __init__(self, name):
                self.name = name
                self.index = None

            def _ensure(self):
                if self.index is None:
                    self.index = acmekit.Index(self.name)

            def search(self, vector):
                self._ensure()
                return self.index.query(vector)
        """,
    )
    report = apply_sites(
        sites_from_payload(
            _removed_method_payload("store.py", 14, "self.index.query", "acmekit.Index.query")
        ),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            client=FakeModelClient(responses=[]),
            fixtures=fixtures,
        ),
    )
    (result,) = report.results
    assert result.status == SiteStatus.SKIPPED
    assert result.status != SiteStatus.NEEDS_HUMAN
    assert result.status != SiteStatus.FIXED_VERIFIED


def test_local_variable_receiver_traced_to_constructor(tmp_path: Path):
    """A local ``index = acmekit.Index(...)`` receiver is fine when query survives."""
    fixtures = _relocated_index_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "use.py",
        """\
        import acmekit

        def search(name, vector):
            index = acmekit.Index(name)
            return index.query(vector)
        """,
    )
    report = apply_sites(
        sites_from_payload(
            _removed_method_payload("use.py", 5, "index.query", "acmekit.Index.query")
        ),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            client=FakeModelClient(responses=[]),
            fixtures=fixtures,
        ),
    )
    (result,) = report.results
    assert result.status == SiteStatus.SKIPPED
    assert result.status != SiteStatus.NEEDS_HUMAN


def test_parameter_receiver_stays_needs_human(tmp_path: Path):
    """A receiver passed in as a parameter cannot be traced — needs-human."""
    fixtures = _relocated_index_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "use.py",
        """\
        def search(index, vector):
            return index.query(vector)
        """,
    )
    report = apply_sites(
        sites_from_payload(
            _removed_method_payload("use.py", 2, "index.query", "acmekit.Index.query")
        ),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            client=FakeModelClient(responses=[]),
            fixtures=fixtures,
        ),
    )
    (result,) = report.results
    assert result.status == SiteStatus.NEEDS_HUMAN
    assert result.status != SiteStatus.FIXED_VERIFIED


def test_method_gone_on_new_class_stays_needs_human(tmp_path: Path):
    """A method that really disappeared on the relocated class stays needs-human."""
    fixtures = _relocated_index_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "store.py",
        """\
        import acmekit

        class Store:
            def __init__(self, name):
                self.index = acmekit.Index(name)

            def scan(self):
                return self.index.legacy_scan()
        """,
    )
    report = apply_sites(
        sites_from_payload(
            _removed_method_payload(
                "store.py", 8, "self.index.legacy_scan", "acmekit.Index.legacy_scan"
            )
        ),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            client=FakeModelClient(responses=[]),
            fixtures=fixtures,
        ),
    )
    (result,) = report.results
    assert result.status == SiteStatus.NEEDS_HUMAN
    assert result.status != SiteStatus.SKIPPED
    assert result.status != SiteStatus.FIXED_VERIFIED
