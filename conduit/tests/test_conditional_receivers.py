"""A name bound to a conditional is every SDK receiver in its branches.

``endpoint = client.beta.items if use_beta else client.items`` then
``endpoint.create(...)`` is a call site of both resources. A release that
removes ``old_param`` from those methods hits that line. The same split
covers ``or``, ``and``, and a walrus. Made-up crate SDK only.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from conduit.impact.api import diff_wheels
from conduit.impact.intersect import intersect
from conduit.outside_in.graph import analyze_python
from conduit.outside_in.specs import ServiceSpec

_SPEC = ServiceSpec(service_id="crate", package="crate", modules=("crate",))

_OLD = """
class Items:
    def create(self, *, old_param: int = 0, label: str = ""):
        return label

class BetaItems:
    def create(self, *, old_param: int = 0, label: str = ""):
        return label

class Beta:
    @property
    def items(self) -> "BetaItems":
        return BetaItems()

class Client:
    def __init__(self, token: str = ""):
        self.token = token

    @property
    def items(self) -> "Items":
        return Items()

    @property
    def beta(self) -> "Beta":
        return Beta()
"""

_NEW = _OLD.replace("old_param: int = 0, ", "")

_APP = """
from crate.client import Client

def send(use_beta):
    client = Client()
    endpoint = client.beta.items if use_beta else client.items
    return endpoint.create(old_param=1)

def either():
    client = Client()
    endpoint = client.items or client.beta.items
    return endpoint.create(old_param=1)

def both(flag):
    client = Client()
    endpoint = flag and client.items
    return endpoint.create(old_param=1)

def picked():
    client = Client()
    endpoint = (chosen := client.items)
    return chosen.create(old_param=1)

def nested(flag):
    client = Client()
    endpoint = client.items or (client.beta.items if flag else client.items)
    return endpoint.create(old_param=1)

def inline(use_beta):
    client = Client()
    return (client.beta.items if use_beta else client.items).create(old_param=1)

def walrus_call(use_beta):
    client = Client()
    return (endpoint := client.beta.items if use_beta else client.items).create(old_param=1)

class Holder:
    def __init__(self, use_beta):
        client = Client()
        self.endpoint = client.beta.items if use_beta else client.items

    def go(self):
        return self.endpoint.create(old_param=1)

def direct():
    client = Client()
    client.beta.items.create(old_param=1)
    return client.items.create(old_param=1)

def with_default():
    client = Client()
    endpoint = client.items or fallback()
    return endpoint.create(old_param=1)

def local_only(flag):
    left = object()
    right = object()
    endpoint = left if flag else right
    return endpoint.create(old_param=1)
"""

_BOTH = {
    "crate.client.Client.beta.items.create",
    "crate.client.Client.items.create",
}
_ITEMS = {"crate.client.Client.items.create"}


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _calls_by_function(text: str) -> dict[str, list[int]]:
    current = "<module>"
    found: dict[str, list[int]] = {}
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("def "):
            current = stripped.split("(", 1)[0][4:]
        elif stripped.startswith("class "):
            current = stripped.split("(", 1)[0][6:].rstrip(":").strip()
        if "create(old_param=1)" in line:
            found.setdefault(current, []).append(index)
    return found


def _methods(mapped, symbol: str) -> set[str]:
    return {
        chain.sdk_method
        for chain in mapped.chains
        if chain.call_symbol == symbol and chain.sdk_method.endswith(".create") and not chain.via_wrapper
    }


def test_a_conditional_name_is_each_sdk_receiver(tmp_path: Path):
    _write(tmp_path / "old" / "crate" / "__init__.py", "from .client import Client\n")
    _write(tmp_path / "old" / "crate" / "client.py", _OLD)
    _write(tmp_path / "new" / "crate" / "__init__.py", "from .client import Client\n")
    _write(tmp_path / "new" / "crate" / "client.py", _NEW)
    app = tmp_path / "shop"
    _write(app / "send.py", _APP)
    text = (app / "send.py").read_text(encoding="utf-8")
    mapped = analyze_python(app, (_SPEC,))

    assert _methods(mapped, "send") == _BOTH
    assert _methods(mapped, "either") == _BOTH
    assert _methods(mapped, "nested") == _BOTH
    assert _methods(mapped, "inline") == _BOTH
    assert _methods(mapped, "walrus_call") == _BOTH
    assert _methods(mapped, "Holder.go") == _BOTH
    assert _methods(mapped, "direct") == _BOTH
    assert _methods(mapped, "picked") == _ITEMS
    assert _methods(mapped, "both") == _ITEMS
    assert _methods(mapped, "with_default") == _ITEMS
    assert _methods(mapped, "local_only") == set()

    calls = _calls_by_function(text)
    local_line = calls["local_only"][0]
    assert any(
        item.lineno == local_line and item.expr == "endpoint.create" for item in mapped.unresolved_calls
    )
    assert not any(item.expr == "self.endpoint.create" for item in mapped.unresolved_calls)

    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    impacts = intersect(
        list(changes),
        mapped.chains,
        dependency="crate",
        from_version="1.0.0",
        to_version="2.0.0",
        return_classes=changes.return_classes,
        surviving=changes.surviving,
        methods=changes.methods,
    )
    flagged = {
        site.line
        for item in impacts
        if item.kind == "param_removed" and item.old == "old_param"
        for site in item.sites
        if not site.via_wrapper and site.path == "send.py"
    }
    expect = [
        line
        for name, lines in calls.items()
        if name != "local_only"
        for line in lines
    ]
    assert expect
    assert set(expect) <= flagged
    assert local_line not in flagged
