"""The recheck's argument-type law: generic, and silent when a type is unknown."""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

from conduit.apply import engine as engine_mod
from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.typecheck import argument_breaks

from test_apply import _impact_payload, _repo_with_break, _settings

# A made-up routing library: nothing here is any real SDK's API.
_LIBRARY = {
    "geo_router/__init__.py": "from .router import Router\n",
    "geo_router/types.py": """
        from typing import Literal, TypedDict
        from typing_extensions import NotRequired, Required

        class Point(TypedDict):
            lat: float
            lon: float

        class Stop(Point):
            name: str
            wait: NotRequired[int]

        class Hints(TypedDict, total=False):
            avoid: str
            key: Required[str]

        Mode = Literal["car", "bike"]
        """,
    "geo_router/router.py": """
        from typing import Iterable, Optional, Sequence, Union
        from .types import Hints, Mode, Stop

        class Router:
            def route(
                self,
                *,
                stops: Sequence[Stop],
                mode: Mode,
                limit: int,
                label: str,
                tags: Iterable[str] = (),
                note: Optional[str] = None,
                size: Union[int, str] = 1,
                hints: Hints = {},
            ) -> str:
                return ""
        """,
}


def _modules() -> dict[str, ast.Module]:
    out = {}
    for name, text in _LIBRARY.items():
        module = name[:-3].replace("/", ".").removesuffix(".__init__")
        out[module] = ast.parse(textwrap.dedent(text))
    return out


def _breaks(body: str) -> list[str]:
    source = textwrap.dedent(body)
    tree = ast.parse(source)
    line = next(node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "route")
    chain = SimpleNamespace(sdk_method="geo_router.router.Router.route", call_lineno=line, call_expr="router.route")
    return argument_breaks(_modules(), tree, "app.py", chain, "geo-router", "3.0")


def _flagged(body: str) -> set[str]:
    return {text.split(": ", 1)[1].split("=", 1)[0] for text in _breaks(body)}


def test_plain_mismatches_are_flagged():
    found = _breaks(
        """
        def plan(router, city: str):
            text = "Paris" + city
            return router.route(stops=text, mode="plane", limit="10", label=7, note=None, tags=["a"])
        """
    )
    flagged = {text.split(": ", 1)[1].split("=", 1)[0] for text in found}
    assert flagged == {"stops", "mode", "limit", "label"}
    stops = next(text for text in found if "stops=" in text)
    assert "passes a str but Router.route in geo-router 3.0 expects Sequence[Stop]" in stops
    assert "'plane'" in next(text for text in found if "mode=" in text)


def test_none_to_a_non_optional_parameter():
    assert _flagged("def f(router):\n    return router.route(limit=None, note=None, size='x')\n") == {"limit"}


def test_dict_missing_required_typed_dict_keys_including_inherited_ones():
    found = _breaks(
        """
        def f(router):
            return router.route(stops=[{"name": "a", "lat": 1.0}], hints={"avoid": "tolls"})
        """
    )
    assert len(found) == 2
    assert "missing required key(s) lon of Stop" in found[0]
    assert "missing required key(s) key of Hints" in found[1]


def test_complete_or_spread_dicts_are_not_flagged():
    assert _flagged(
        """
        def f(router, extra):
            return router.route(
                stops=[{"name": "a", "lat": 1.0, "lon": 2.0}, {**extra, "name": "b"}],
                hints={"key": "k"},
            )
        """
    ) == set()


def test_unknown_types_are_never_flagged():
    assert _flagged(
        """
        def f(router, raw, flag):
            if flag:
                picked = [raw]
            else:
                picked = "one"
            return router.route(stops=raw, mode=pick(), limit=raw.count, label=helper(raw), tags=picked)
        """
    ) == set()


def test_a_known_return_annotation_is_used():
    assert _flagged(
        """
        def render(points) -> str:
            return ""

        def f(router, points):
            stops = render(points)
            return router.route(stops=stops)
        """
    ) == {"stops"}


def test_same_type_mismatch_twice_goes_to_a_human_after_one_retry(tmp_path: Path, monkeypatch):
    root = _repo_with_break(tmp_path)
    finding = "mod.py:7 client.new_call: items= passes a str but Client.new_call in example-sdk 2.0 expects Iterable[ItemParam]"
    monkeypatch.setattr(engine_mod, "remaining_breaks", lambda *a, **k: [(finding, "example_sdk.Client.new_call")])
    edit = json.dumps({"function_source": "def broken(client):\n    return client.new_call(1, items='x')\n", "new_imports": []})
    client = FakeModelClient(responses=[edit, edit, edit, "why"])
    report = apply_sites(
        sites_from_payload(_impact_payload()),
        ApplyConfig(root=root, settings=_settings(max_attempts=3), client=client, recheck_impact=True),
    )
    result = report.results[0]
    assert result.status == SiteStatus.NEEDS_HUMAN
    assert result.attempts == 2
    assert result.error.startswith("argument type still incompatible after one retry")
