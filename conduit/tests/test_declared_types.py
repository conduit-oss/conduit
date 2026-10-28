"""Declared types along attribute chains, compared by where each class is defined.

A made-up parcel SDK returns a ``Reply`` whose ``weight`` field is the SDK's own
``Weight``; the made-up shop repo has a different class that is also called
``Weight``. Passing ``reply.weight`` where the shop declares its own ``Weight``
is a mismatch; a subclass of the declared class is not; anything that cannot be
read from declarations is never reported.
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

from conduit.apply import engine as engine_mod
from conduit.apply.client import FakeModelClient
from conduit.apply.declared import declared_type_breaks
from conduit.apply.engine import ApplyConfig, _MISMATCH, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.unused import drop_unused_imports

from test_apply import _impact_payload, _settings

_SDK = {
    "parcel_sdk/__init__.py": "from .types import Reply, Weight\n",
    "parcel_sdk/_base.py": """
        class Model:
            pass
        """,
    "parcel_sdk/types/__init__.py": """
        from .reply import Reply
        from .weight import Weight
        from .payload import Payload
        """,
    "parcel_sdk/types/weight.py": """
        from .._base import Model

        class Weight(Model):
            kilos: float
            tare_kilos: float
        """,
    "parcel_sdk/types/reply.py": """
        from typing import Optional
        from .._base import Model
        from .weight import Weight

        class Reply(Model):
            label: str
            weight: Weight
            spare: Optional[Weight]

            @property
            def first(self) -> "Weight":
                return self.weight
        """,
    "parcel_sdk/types/payload.py": """
        from typing import TypedDict
        from .weight import Weight

        class Payload(TypedDict):
            weight: Weight
        """,
    "parcel_sdk/ship.py": """
        from .types.weight import Weight

        def ship(weight: Weight, note: str = "") -> str:
            return ""
        """,
}

_SHOP = {
    "shop/__init__.py": "",
    "shop/records.py": """
        from __future__ import annotations

        from dataclasses import dataclass
        from typing import Optional, Union

        from parcel_sdk.types import Weight as ParcelWeight
        from elsewhere import Outside


        class Weight:
            def __init__(self, grams: int, tare_grams: int = 0) -> None:
                self.grams = grams
                self.tare_grams = tare_grams

            @staticmethod
            def from_kilos(kilos: float) -> Weight:
                return Weight(int(kilos * 1000))


        class HeavyWeight(Weight):
            pass


        class Record:
            def __init__(self, weight: Weight, note: str = "") -> None:
                self.weight = weight


        class Either:
            def __init__(self, weight: Union[Weight, ParcelWeight]) -> None:
                self.weight = weight


        class Maybe:
            def __init__(self, weight: Optional[Weight] = None) -> None:
                self.weight = weight


        @dataclass
        class Entry:
            weight: Weight
            note: str = ""


        class Row(Outside):
            weight: Weight


        class Holder:
            heavy: HeavyWeight

            @property
            def held(self) -> HeavyWeight:
                return self.heavy


        def keep(weight: Weight) -> Record:
            return Record(weight)
        """,
}


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return root


def _sdk() -> dict[str, ast.Module]:
    return {
        name[:-3].replace("/", ".").removesuffix(".__init__"): ast.parse(textwrap.dedent(text))
        for name, text in _SDK.items()
    }


_HEAD = """
from __future__ import annotations

from typing import Any, Optional

from parcel_sdk.types import Payload, Reply
from parcel_sdk.ship import ship
from shop.records import Either, Entry, Holder, HeavyWeight, Maybe, Record, Row, Weight, keep
"""


def _findings(tmp_path: Path, body: str, *, original: str = "", traced=None) -> list[str]:
    root = _write(tmp_path, _SHOP)
    source = _HEAD + textwrap.dedent(body)
    (root / "shop" / "use.py").write_text(source, encoding="utf-8")
    span = (1, len(source.splitlines()))
    return declared_type_breaks(_sdk(), root, "shop/use.py", source, span, original, traced)


def test_a_release_class_passed_where_a_same_named_repo_class_is_declared(tmp_path: Path):
    found = _findings(
        tmp_path,
        """
        def build(reply: Reply) -> Record:
            return Record(weight=reply.weight)
        """,
    )
    assert len(found) == 1
    text = found[0]
    assert "weight= passes parcel_sdk.types.weight.Weight (from reply.weight)" in text
    assert "Record in this repo expects shop.records.Weight" in text
    assert "only shares the name Weight" in text
    assert "(shop.records.Weight takes: grams: int, tare_grams: int)" in text
    assert "(parcel_sdk.types.weight.Weight fields: kilos: float, tare_kilos: float)" in text
    assert "Weight.from_kilos(kilos: float)" in text
    match = _MISMATCH.match(text)
    assert match is not None and match.group("kw") == "weight" and match.group("owner") == "Record"


def test_a_subclass_of_the_declared_class_is_allowed(tmp_path: Path):
    assert _findings(
        tmp_path,
        """
        def build(holder: Holder, heavy: HeavyWeight) -> None:
            Record(weight=holder.held)
            Record(weight=holder.heavy)
            Record(heavy)
            keep(HeavyWeight(5))
        """,
    ) == []


def test_the_repo_class_itself_or_a_mapped_value_is_allowed(tmp_path: Path):
    assert _findings(
        tmp_path,
        """
        def build(reply: Reply) -> None:
            Record(weight=Weight(grams=int(reply.weight.kilos * 1000)))
            Record(weight=Weight.from_kilos(reply.weight.kilos))
            mapped = Weight.from_kilos(reply.first.kilos)
            Record(weight=mapped)
        """,
    ) == []


def test_unknown_values_and_targets_are_never_reported(tmp_path: Path):
    assert _findings(
        tmp_path,
        """
        def build(reply, other: Any, outside: Outside, flag: bool) -> None:
            Record(weight=reply.weight)
            Record(weight=other.weight)
            Record(weight=outside.weight)
            Record(weight=helper(reply))
            picked = Reply() if flag else other
            if flag:
                picked = other
            Record(weight=picked.weight)
            Record(**other)
        """,
    ) == []


def test_optional_and_union_on_both_sides(tmp_path: Path):
    found = _findings(
        tmp_path,
        """
        def build(reply: Optional[Reply]) -> None:
            Maybe(weight=reply.spare)
            Either(weight=reply.spare)
            Either(weight=Weight(1))
        """,
    )
    assert len(found) == 1
    assert "Maybe(...): weight= passes parcel_sdk.types.weight.Weight (from reply.spare)" in found[0]


def test_property_typed_dict_key_and_field_constructors(tmp_path: Path):
    found = _findings(
        tmp_path,
        """
        def build(reply: Reply, payload: Payload) -> None:
            Record(weight=reply.first)
            Record(weight=payload["weight"])
            Entry(weight=reply.weight)
            Entry(reply.weight)
            Row(weight=reply.weight)
        """,
    )
    assert [text.split(": weight=")[0].split(" ", 1)[1] for text in found] == [
        "Record(...)",
        "Record(...)",
        "Entry(...)",
        "Entry(...)",
        "Row(...)",
    ]
    assert "(shop.records.Weight takes: grams: int, tare_grams: int)" in found[2]


def test_a_mismatch_the_original_code_already_had_is_not_the_rewrites(tmp_path: Path):
    body = """
        def build(reply: Reply) -> Record:
            return Record(weight=reply.weight)
        """
    assert _findings(tmp_path, body, original=_HEAD + textwrap.dedent(body)) == []


def test_a_repo_value_passed_into_a_release_callable(tmp_path: Path):
    sdk = _sdk()
    node = next(item for item in sdk["parcel_sdk.ship"].body if isinstance(item, ast.FunctionDef))
    body = """
        def build(weight: Weight, reply: Reply) -> None:
            ship(reply.weight)
            ship(weight)
        """
    line = (_HEAD + textwrap.dedent(body)).splitlines().index("    ship(weight)") + 1
    found = _findings(tmp_path, body, traced={(line, "ship"): ("parcel_sdk.ship", node)})
    assert len(found) == 1
    assert "weight= passes shop.records.Weight (from weight) but ship in this repo" not in found[0]
    assert "but ship in the parcel_sdk release expects parcel_sdk.types.weight.Weight" in found[0]


def test_a_repeated_class_mismatch_is_handed_off_after_one_retry(tmp_path: Path, monkeypatch):
    found = _findings(
        tmp_path / "shop-repo",
        """
        def build(reply: Reply) -> Record:
            return Record(weight=reply.weight)
        """,
    )
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mod.py").write_text(
        "import example_sdk\n\n\nclient = None\n\n\ndef broken(client):\n    return client.old_call(1)\n",
        encoding="utf-8",
    )
    text = found[0].replace("shop/use.py:", "mod.py:", 1)
    monkeypatch.setattr(engine_mod, "remaining_breaks", lambda *a, **k: [(text, "")])
    edit = json.dumps({"function_source": "def broken(client):\n    return client.new_call(1)\n", "new_imports": []})
    client = FakeModelClient(responses=[edit, edit, edit, "why"])
    report = apply_sites(
        sites_from_payload(_impact_payload()),
        ApplyConfig(root=root, settings=_settings(max_attempts=3), client=client, recheck_impact=True),
    )
    result = report.results[0]
    assert result.status == SiteStatus.NEEDS_HUMAN
    assert result.attempts == 2
    assert result.error.startswith("argument type still incompatible after one retry")
    assert "expects shop.records.Weight" in result.error
    assert "takes: grams: int" in str(client.calls[1])  # the retry is told the target's fields


# --- imports a rewrite left unused ------------------------------------------------------

_BEFORE = """\
from parcel_sdk.flags import UNSET, KEEP
from parcel_sdk.flags import SPARE
import os


def send(client):
    return client.go(a=UNSET, b=KEEP, c=SPARE)
"""


def test_an_import_the_rewrite_stopped_using_is_dropped():
    after = _BEFORE.replace("client.go(a=UNSET, b=KEEP, c=SPARE)", "client.go(b=KEEP)")
    cleaned, span = drop_unused_imports(_BEFORE, after, (6, 7), "shop/send.py")
    assert cleaned.splitlines()[:2] == ["from parcel_sdk.flags import KEEP", "import os"]
    assert span == (5, 6)
    assert cleaned.splitlines()[span[0] - 1] == "def send(client):"


def test_imports_still_used_or_never_used_are_kept():
    after = _BEFORE.replace("client.go(a=UNSET, b=KEEP, c=SPARE)", "client.go(a=UNSET, b=KEEP, c=SPARE)  # same")
    assert drop_unused_imports(_BEFORE, after, (6, 7), "shop/send.py") == (after, (6, 7))
    before = "from parcel_sdk.flags import UNSET\n\n\ndef send(client):\n    return client.go()\n"
    after = before.replace("client.go()", "client.go(1)")
    assert drop_unused_imports(before, after, (4, 5), "shop/send.py") == (after, (4, 5))
    named = _BEFORE.replace("c=SPARE)", "c=None)  # SPARE was here")
    assert "SPARE" in drop_unused_imports(_BEFORE, named, (6, 7), "shop/send.py")[0].splitlines()[1]


def test_a_package_init_keeps_its_imports():
    after = _BEFORE.replace("client.go(a=UNSET, b=KEEP, c=SPARE)", "client.go()")
    assert drop_unused_imports(_BEFORE, after, (6, 7), "shop/__init__.py") == (after, (6, 7))


def test_a_name_another_file_imports_from_this_module_is_kept(tmp_path: Path):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "send.py").write_text(_BEFORE, encoding="utf-8")
    (tmp_path / "shop" / "other.py").write_text("from shop.send import UNSET\n", encoding="utf-8")
    after = _BEFORE.replace("client.go(a=UNSET, b=KEEP, c=SPARE)", "client.go(b=KEEP)")
    cleaned, _span = drop_unused_imports(_BEFORE, after, (6, 7), "shop/send.py", tmp_path)
    assert cleaned.splitlines()[:2] == ["from parcel_sdk.flags import UNSET, KEEP", "import os"]
