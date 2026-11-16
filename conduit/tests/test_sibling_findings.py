"""Sibling findings in one function reach the lead pack; a skipped method lead
does not swallow the constructor.

Made-up packages only (acmekit). No real SDK, product, or migration-table names.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.apply import verify as verify_mod
from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.typecheck import parameter_brief
from conduit.apply.signatures import _modules

from test_apply import _hits_in_span, _sent, _settings


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _acmekit_wheels(tmp_path: Path) -> Path:
    """acmekit 1.x → 2.x: init gone, Store args change, create_store needs StoreSpec,
    search survives on the relocated Store."""
    old = tmp_path / "fixtures" / "old"
    new = tmp_path / "fixtures" / "new"
    _write(
        old / "acmekit" / "__init__.py",
        """\
        def init(api_key=None):
            return None

        def create_store(name):
            return Store(name)

        class Store:
            def __init__(self, name):
                self.name = name

            def search(self, q):
                return []
        """,
    )
    _write(
        new / "acmekit" / "__init__.py",
        """\
        from .client import Client, Store, StoreSpec
        """,
    )
    _write(
        new / "acmekit" / "client.py",
        """\
        from typing import TypedDict

        class StoreSpec(TypedDict):
            name: str
            region: str

        class Store:
            def __init__(self, api_key: str, host: str = "local"):
                self.api_key = api_key
                self.host = host

            def search(self, q: str):
                return []

        class Client:
            def __init__(self, api_key: str):
                self.api_key = api_key

            def create_store(self, name: str, spec: StoreSpec) -> Store:
                return Store(self.api_key)

            def Store(self, api_key: str, host: str = "local") -> Store:
                return Store(api_key, host=host)
        """,
    )
    fixtures = tmp_path / "fixtures"
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"acmekit": {"wheels": {"1.0": "old", "2.0": "new"}}}}),
        encoding="utf-8",
    )
    return fixtures


def _pack_json(client: FakeModelClient, call: int = 0) -> dict:
    text = _sent(client, call)
    return json.loads(text.split("Context pack (JSON):", 1)[1].rsplit("Return JSON", 1)[0])


def _impact(
    *rows: tuple[str, int, str, str, str, list[dict] | None],
    path: str = "mod.py",
) -> dict:
    """Build a multi-change impact payload. Each row is
    (change, line, expr, symbol, kind, candidates)."""
    impacts = []
    for change, line, expr, symbol, kind, candidates in rows:
        item = {
            "dependency": "acmekit",
            "from_version": "1.0",
            "to_version": "2.0",
            "change": change,
            "confidence": "high",
            "source": "wheel",
            "kind": kind,
            "symbol": symbol,
            "old": symbol,
            "new": "",
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
        if candidates:
            item["candidates"] = candidates
        impacts.append(item)
    return {
        "dependency": "acmekit",
        "impacts": impacts,
        "deprecations": [],
        "diffed": ["acmekit"],
        "notes": [],
    }


def _both_breaks(scratch: Path, site, *, span, fixtures, allow_network, original="") -> list[tuple[str, str]]:
    """Recheck stand-in: flag leftover init or old Store(name) in the rewritten span."""
    lines = (scratch / site.path).read_text(encoding="utf-8").splitlines()
    hits: list[tuple[str, str]] = []
    for number, line in enumerate(lines, start=1):
        if not (span[0] <= number <= span[1]):
            continue
        if "acmekit.init" in line:
            hits.append(
                (f"{site.path}:{number} acmekit.init: init removed", "acmekit.init")
            )
        if "acmekit.Store(" in line and "api_key" not in line:
            hits.append(
                (
                    f"{site.path}:{number} acmekit.Store: constructor args changed",
                    "acmekit.Store",
                )
            )
    return hits


def test_sibling_init_and_store_both_in_lead_pack(tmp_path: Path, monkeypatch):
    """Constructor + module-init siblings: lead prompt carries both; partial rewrite fails."""
    fixtures = _acmekit_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "mod.py",
        """\
        import acmekit

        def connect(name):
            acmekit.init(api_key="k")
            return acmekit.Store(name)
        """,
    )
    payload = _impact(
        (
            "init removed; use Client(api_key=...)",
            4,
            "acmekit.init",
            "acmekit.init",
            "removed",
            [{"symbol": "acmekit.Client", "signature": "Client(api_key)"}],
        ),
        (
            "Store constructor requires api_key",
            5,
            "acmekit.Store",
            "acmekit.Store",
            "removed",
            [{"symbol": "acmekit.Client.Store", "signature": "Client.Store(api_key, host=...)"}],
        ),
    )
    monkeypatch.setattr(verify_mod, "_breaks_listed", _both_breaks)
    # Keep the model path: mechanical import relocation would claim init first.
    monkeypatch.setattr("conduit.apply.engine._load_import_maps", lambda *a, **k: [])
    only_lead = json.dumps(
        {
            "function_source": (
                "def connect(name):\n"
                "    client = Client(api_key=\"k\")\n"
                "    return acmekit.Store(name)\n"
            ),
            "new_imports": ["from acmekit import Client"],
        }
    )
    both = json.dumps(
        {
            "function_source": (
                "def connect(name):\n"
                "    client = Client(api_key=\"k\")\n"
                "    return client.Store(api_key=\"k\")\n"
            ),
            "new_imports": ["from acmekit import Client"],
        }
    )
    client = FakeModelClient(responses=[only_lead, both, "why"])
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            write=True,
            client=client,
            recheck_impact=True,
            fixtures=fixtures,
        ),
    )
    pack = _pack_json(client, 0)
    release = pack["release_diff"]
    siblings = release.get("sibling_findings") or []
    assert len(siblings) == 1, release
    changes = {release["change"], siblings[0]["change"]}
    assert any("init" in c for c in changes)
    assert any("Store" in c for c in changes)
    sigs = release.get("new_release_signatures") or {}
    requires = release.get("new_release_requires") or []
    blob = json.dumps(sigs) + "\n".join(requires)
    assert "Client" in blob or "api_key" in blob
    assert any(r.status in {SiteStatus.FIXED_UNVERIFIED, SiteStatus.FIXED_VERIFIED} for r in report.results), [
        (r.status, r.error) for r in report.results
    ]
    # Partial rewrite was rejected; the accepted one cleared both.
    assert len(client.calls) >= 2
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "acmekit.init" not in text
    assert "acmekit.Store(name)" not in text


def test_sibling_replacement_type_reaches_lead_pack(tmp_path: Path, monkeypatch):
    """StoreSpec required by a sibling replacement appears in the lead pack."""
    fixtures = _acmekit_wheels(tmp_path)
    # Confirm the wheel brief expands StoreSpec (same helper the engine uses).
    brief = parameter_brief(_modules(str(fixtures / "new")), "acmekit.Client.create_store")
    assert "StoreSpec" in brief
    assert "name: str" in brief

    root = tmp_path / "app"
    _write(
        root / "mod.py",
        """\
        import acmekit

        def provision(name):
            acmekit.create_store(name)
            return acmekit.Store(name)
        """,
    )
    payload = _impact(
        (
            "Store constructor requires api_key",
            5,
            "acmekit.Store",
            "acmekit.Store",
            "removed",
            [{"symbol": "acmekit.Client.Store", "signature": "Client.Store(api_key, host=...)"}],
        ),
        (
            "create_store moved onto Client and requires StoreSpec",
            4,
            "acmekit.create_store",
            "acmekit.create_store",
            "removed",
            [
                {
                    "symbol": "acmekit.Client.create_store",
                    "signature": "Client.create_store(name, spec: StoreSpec)",
                }
            ],
        ),
    )
    monkeypatch.setattr(verify_mod, "_breaks_listed", _hits_in_span)
    monkeypatch.setattr("conduit.apply.engine._load_import_maps", lambda *a, **k: [])
    edit = json.dumps(
        {
            "function_source": (
                "def provision(name):\n"
                "    client = Client(api_key=\"k\")\n"
                "    client.create_store(name, spec={\"name\": name, \"region\": \"us\"})\n"
                "    return client.Store(api_key=\"k\")\n"
            ),
            "new_imports": ["from acmekit import Client"],
        }
    )
    client = FakeModelClient(responses=[edit, "why"])
    apply_sites(
        sites_from_payload(payload),
        ApplyConfig(
            root=root,
            settings=_settings(),
            client=client,
            recheck_impact=True,
            fixtures=fixtures,
        ),
    )
    pack = _pack_json(client, 0)
    release = pack["release_diff"]
    siblings = release.get("sibling_findings") or []
    assert siblings, release
    assert any("create_store" in (s.get("change") or "") or s.get("symbol") == "acmekit.create_store" for s in siblings)
    blob = json.dumps(release.get("new_release_signatures") or {}) + "\n".join(
        release.get("new_release_requires") or []
    )
    assert "StoreSpec" in blob


def test_skipped_method_lead_still_prompts_constructor(tmp_path: Path, monkeypatch):
    """Impact lists search first; surviving-method skip must not leave Store covered."""
    fixtures = _acmekit_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "mod.py",
        """\
        import acmekit

        def lookup(name, q):
            store = acmekit.Store(name)
            return store.search(q)
        """,
    )
    payload = _impact(
        (
            "search removed from old Store home",
            5,
            "store.search",
            "acmekit.Store.search",
            "removed",
            [],
        ),
        (
            "Store constructor requires api_key",
            4,
            "acmekit.Store",
            "acmekit.Store",
            "removed",
            [{"symbol": "acmekit.Client.Store", "signature": "Client.Store(api_key, host=...)"}],
        ),
    )

    def fake_survive(source, *, line, expr, symbol, kind, package, maps=()):
        if symbol.endswith(".search") or (expr or "").endswith(".search"):
            return (
                f"{expr or 'search'} still resolves via relocated Store; "
                "rewrite the constructor, not this call"
            )
        return None

    monkeypatch.setattr(
        "conduit.apply.engine.method_survives_on_relocated_receiver", fake_survive
    )
    monkeypatch.setattr(verify_mod, "_breaks_listed", lambda *a, **k: [])
    monkeypatch.setattr("conduit.apply.engine._load_import_maps", lambda *a, **k: [])
    edit = json.dumps(
        {
            "function_source": (
                "def lookup(name, q):\n"
                "    store = Client(api_key=\"k\").Store(api_key=\"k\")\n"
                "    return store.search(q)\n"
            ),
            "new_imports": ["from acmekit import Client"],
        }
    )
    client = FakeModelClient(responses=[edit, "why"])
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            client=client,
            recheck_impact=True,
            fixtures=fixtures,
        ),
    )
    statuses = [(r.site.expr, r.status, r.why or r.error) for r in report.results]
    assert any(
        r.status in {SiteStatus.FIXED_UNVERIFIED, SiteStatus.FIXED_VERIFIED}
        and r.site.expr == "acmekit.Store"
        for r in report.results
    ), statuses
    assert any(
        r.status == SiteStatus.SKIPPED and "constructor" in (r.why or "").lower()
        for r in report.results
    ), statuses
    assert client.calls, "constructor must still be proposed after method skip"
    assert "acmekit.Store(name)" not in (root / "mod.py").read_text(encoding="utf-8")


def test_sibling_controls_single_separate_and_wrapper(tmp_path: Path, monkeypatch):
    """Controls: one finding unchanged; other functions stay separate; wrappers skipped."""
    fixtures = _acmekit_wheels(tmp_path)
    root = tmp_path / "app"
    _write(
        root / "mod.py",
        """\
        import acmekit

        def alone(name):
            return acmekit.Store(name)

        def other(name):
            return acmekit.Store(name)

        def wrap(name):
            return acmekit.Store(name)
        """,
    )
    payload = {
        "dependency": "acmekit",
        "impacts": [
            {
                "dependency": "acmekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "Store constructor requires api_key",
                "confidence": "high",
                "source": "wheel",
                "kind": "removed",
                "symbol": "acmekit.Store",
                "old": "acmekit.Store",
                "new": "",
                "candidates": [
                    {"symbol": "acmekit.Client.Store", "signature": "Client.Store(api_key)"}
                ],
                "sites": [
                    {
                        "path": "mod.py",
                        "line": 4,
                        "expr": "acmekit.Store",
                        "confidence": "high",
                        "via_wrapper": False,
                    },
                    {
                        "path": "mod.py",
                        "line": 7,
                        "expr": "acmekit.Store",
                        "confidence": "high",
                        "via_wrapper": False,
                    },
                    {
                        "path": "mod.py",
                        "line": 10,
                        "expr": "acmekit.Store",
                        "confidence": "high",
                        "via_wrapper": True,
                    },
                ],
            }
        ],
        "deprecations": [],
        "diffed": ["acmekit"],
        "notes": [],
    }
    monkeypatch.setattr(verify_mod, "_breaks_listed", lambda *a, **k: [])
    monkeypatch.setattr("conduit.apply.engine._load_import_maps", lambda *a, **k: [])
    alone_edit = json.dumps(
        {
            "function_source": "def alone(name):\n    return Client(api_key=\"k\").Store(api_key=\"k\")\n",
            "new_imports": ["from acmekit import Client"],
        }
    )
    other_edit = json.dumps(
        {
            "function_source": "def other(name):\n    return Client(api_key=\"k\").Store(api_key=\"k\")\n",
            "new_imports": ["from acmekit import Client"],
        }
    )
    client = FakeModelClient(responses=[alone_edit, "why", other_edit, "why"])
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            client=client,
            recheck_impact=True,
            fixtures=fixtures,
        ),
    )
    packs = [_pack_json(client, i) for i in range(0, len(client.calls), 2)]
    assert all(not (p["release_diff"].get("sibling_findings") or []) for p in packs)
    assert len(packs) == 2
    wrapper = next(r for r in report.results if r.site.via_wrapper)
    assert wrapper.status == SiteStatus.SKIPPED
    assert "wrapper" in wrapper.why.lower()
