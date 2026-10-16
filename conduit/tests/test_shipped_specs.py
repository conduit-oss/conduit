"""Versioned specs inside a dependency wheel, and rename grounding."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from conduit.feeds.model import FeedHit
from conduit.feeds.names import rename_grounded
from conduit.feeds.targets import WatchTarget
from conduit.feeds.lookup import ServiceFeeds
from conduit.feeds.watch import _ground_renames
from conduit.impact.run import _versions
from conduit.impact.shipped_specs import closed_pins, diff_shipped_specs
from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.model import Chain


def _wheel(path: Path, name: str, version: str, requires: list[str], files: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    for req in requires:
        meta += f"Requires-Dist: {req}\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA", meta)
        archive.writestr(f"{name}/__init__.py", "class Client:\n    pass\n")
        for arc, body in files.items():
            archive.writestr(arc, body)


def _model(members: list[str]) -> bytes:
    shapes = {name: {} for name in members}
    payload = {
        "metadata": {"endpointPrefix": "demo", "serviceId": "Demo"},
        "operations": {"PutThing": {"input": {"shape": "In"}, "output": {"shape": "Out"}}},
        "shapes": {
            "In": {"type": "structure", "members": shapes},
            "Out": {"type": "structure", "members": {"ETag": {}}},
        },
    }
    return json.dumps(payload).encode()


def _chain() -> Chain:
    return Chain(
        service="widget",
        package="widget",
        call_rel="app.py",
        call_lineno=4,
        call_symbol="upload",
        call_expr="client.put_thing",
        hops=(),
        sdk_method="demo.put_thing",
        kind="cloud_call",
        params=("ACL",),
        site_keywords=(),
        response_fields=(),
        confidence="high",
        via_wrapper=False,
    )


def test_closed_pin_uses_the_lower_bound_of_a_range():
    text = "\n".join(
        [
            "Requires-Dist: models (>=1.2.0,<1.3.0)",
            "Requires-Dist: extra-tools (>=1,<2) ; extra == 'dev'",
            "Requires-Dist: loose (>=1.0)",
            "Requires-Dist: pinned (==4.5.6)",
        ]
    )
    assert closed_pins(text) == [("models", "1.2.0"), ("pinned", "4.5.6")]


def test_dependency_wheel_spec_maps_a_removed_member(tmp_path: Path):
    old_dep = tmp_path / "deps" / "models-1.0.0-py3-none-any.whl"
    new_dep = tmp_path / "deps" / "models-2.0.0-py3-none-any.whl"
    spec = "data/demo/2010-01-01/service-2.json"
    _wheel(old_dep, "models", "1.0.0", [], {spec: _model(["ACL", "Bucket"])})
    _wheel(new_dep, "models", "2.0.0", [], {spec: _model(["Bucket"])})
    old = tmp_path / "widget-1.0.0-py3-none-any.whl"
    new = tmp_path / "widget-2.0.0-py3-none-any.whl"
    _wheel(old, "widget", "1.0.0", ["models (>=1.0.0,<1.1.0)"], {})
    _wheel(new, "widget", "2.0.0", ["models (>=2.0.0,<2.1.0)"], {})
    changes, compared, note = diff_shipped_specs(
        old,
        new,
        package="widget",
        chains=[_chain()],
        allow_network=False,
        search_dirs=[tmp_path],
    )
    assert compared, note
    assert note == ""
    removed = next(item for item in changes if item.kind == "param_removed")
    assert removed.old == "ACL"
    assert removed.service == "demo"
    assert removed.source == "service-model"


def test_changelog_rename_requires_real_api_names(tmp_path: Path):
    wheel = tmp_path / "impact" / "wheels" / "demo-1.0.0-py3-none-any.whl"
    _wheel(
        wheel,
        "demo",
        "1.0.0",
        [],
        {"demo/client.py": "def complete(limit, page_size):\n    return limit\n"},
    )
    (tmp_path / "impact" / "index.json").write_text(
        json.dumps({"packages": {"demo": {"wheels": {"1.0.0": "wheels/demo-1.0.0-py3-none-any.whl"}}}}),
        encoding="utf-8",
    )
    target = WatchTarget(
        service_id="demo",
        package="demo",
        pypi_version="1.0.0",
        npm_version=None,
        feeds=ServiceFeeds(service_id="demo", package="demo", pypi="demo"),
    )
    hits = [
        FeedHit(
            source="changelog",
            confidence="low",
            package="demo",
            ecosystem="pypi",
            from_version="",
            to_version="",
            change="param_rename",
            old="was",
            new="urllib",
            detail="changelog says parameter was renamed to urllib",
            url="https://example.invalid/changelog",
        ),
        FeedHit(
            source="changelog",
            confidence="low",
            package="demo",
            ecosystem="pypi",
            from_version="",
            to_version="",
            change="param_rename",
            old="limit",
            new="page_size",
            detail="changelog says parameter limit renamed to page_size",
            url="https://example.invalid/changelog",
        ),
    ]
    kept = _ground_renames(hits, [target], fixtures=tmp_path / "impact", allow_network=False)
    assert [(hit.old, hit.new) for hit in kept] == [("limit", "page_size")]
    names = {"limit", "page_size"}
    assert not rename_grounded("was", "urllib", names)
    assert rename_grounded("limit", "page_size", names)


def test_annotated_parameter_resolves_a_cloud_call(tmp_path: Path):
    (tmp_path / "checks.py").write_text(
        "import boto3\n\n"
        "def check(session: boto3.Session):\n"
        "    client = session.client('iam')\n"
        "    return client.get_account_summary()\n",
        encoding="utf-8",
    )
    service_map = analyze_repo(tmp_path)
    methods = {chain.sdk_method for chain in service_map.chains}
    assert "iam.get_account_summary" in methods


def test_open_lower_bound_compares_from_the_oldest_release(tmp_path: Path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["demo>=1.2"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "conduit.impact.versions._pypi_json",
        lambda _package: {
            "info": {"version": "9.0.0"},
            "releases": {
                "1.2.0": [{"packagetype": "bdist_wheel", "size": 10}],
                "3.0.0": [{"packagetype": "bdist_wheel", "size": 10}],
                "9.0.0": [{"packagetype": "bdist_wheel", "size": 10}],
            },
        },
    )
    chosen, resolved_to, note = _versions(
        tmp_path,
        "demo",
        {"demo": "1.2"},
        {},
        [],
        None,
        None,
        explicit=True,
        allow_network=True,
    )
    assert note == ""
    assert chosen == "1.2.0"
    assert resolved_to == "9.0.0"
    hit = FeedHit(
        source="pypi",
        confidence="high",
        package="demo",
        ecosystem="pypi",
        from_version="1.2",
        to_version="9.0.0",
        change="version",
        old="1.2",
        new="9.0.0",
        detail="PyPI demo 1.2 -> 9.0.0",
        url="https://pypi.org/pypi/demo/json",
    )
    watched_from, watched_to, watched_note = _versions(
        tmp_path,
        "demo",
        {"demo": "1.2"},
        {},
        [hit],
        None,
        None,
        explicit=False,
        allow_network=True,
    )
    assert watched_note == ""
    assert watched_from == "1.2.0"
    assert watched_to == "9.0.0"


def test_sdist_only_latest_falls_back_to_a_wheel(monkeypatch):
    from conduit.impact.versions import wheel_backed_version

    monkeypatch.setattr(
        "conduit.impact.versions._pypi_json",
        lambda _package: {
            "info": {"version": "2.1.5"},
            "releases": {
                "2.1.4": [{"packagetype": "bdist_wheel", "size": 100, "filename": "x-py3-none-any.whl"}],
                "2.1.5": [{"packagetype": "sdist", "size": 100, "filename": "x.tar.gz"}],
            },
        },
    )
    assert wheel_backed_version("demo", "2.1.5") == "2.1.4"
