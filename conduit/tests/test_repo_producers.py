"""Repo helpers that already produce a type a new-release callable requires.

A made-up mail SDK: ``send`` once took ``to: str`` and now requires
``recipients: list[Recipient]`` (a TypedDict) and ``window: Window`` (a class).
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

from conduit.apply import verify as verify_mod
from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.producers import repo_producers
from conduit.apply.typecheck import structured_requirements

from test_apply import _sent, _good_edit, _hits_in_span, _impact_payload, _settings


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return root


_SDK = {
    "mail_sdk/__init__.py": "from .outbox import Outbox\n",
    "mail_sdk/types.py": """
        from typing import TypedDict

        class Recipient(TypedDict):
            address: str
            name: str

        class Window:
            def __init__(self, start: int, end: int) -> None:
                self.start, self.end = start, end
        """,
    "mail_sdk/outbox.py": """
        from typing import Optional
        from .types import Recipient, Window

        class Outbox:
            def send(self, *, recipients: list[Recipient], window: Window, subject: str, cc: Optional[str] = None) -> str:
                return ""
        """,
}


def _sdk_modules() -> dict[str, ast.Module]:
    out = {}
    for name, text in _SDK.items():
        out[name[:-3].replace("/", ".").removesuffix(".__init__")] = ast.parse(textwrap.dedent(text))
    return out


def _repo(root: Path) -> Path:
    return _write(
        root,
        {
            "app/addresses.py": """
                from typing import List
                from mail_sdk.types import Recipient

                def as_recipients(rows) -> List["Recipient"]:
                    return [{"address": r[0], "name": r[1]} for r in rows]

                def as_recipients_text(rows) -> str:
                    return ", ".join(r[0] for r in rows)

                def one_recipient(row) -> Recipient:
                    return {"address": row[0], "name": row[1]}
                """,
            "app/digest.py": """
                def pick(rows):
                    return rows

                def gather(rows):
                    return [{"address": r, "name": r} for r in rows]

                def mail(outbox, rows, window):
                    found = gather(rows)
                    return outbox.send(recipients=found, window=window, subject="digest")

                def default_window():
                    return None

                class Clock:
                    def window(self) -> "Window":
                        return None
                """,
            "app/legacy.py": """
                def notify(outbox, rows):
                    return outbox.send(to=", ".join(rows), subject="hi")

                def as_recipients(rows) -> str:
                    return ""
                """,
            "tests/test_helpers.py": """
                def fake_recipients() -> list:
                    return []
                """,
        },
    )


def test_structured_requirements_cover_typed_dict_lists_and_classes():
    reqs = structured_requirements(_sdk_modules(), "mail_sdk.outbox.Outbox.send")
    assert reqs == [
        {"param": "recipients", "annotation": "list[Recipient]", "names": ["Recipient"], "iterable": True},
        {"param": "window", "annotation": "Window", "names": ["Window"], "iterable": False},
    ]
    assert structured_requirements(_sdk_modules(), "mail_sdk.outbox.Outbox.gone") == []


def test_repo_producers_found_by_return_type_and_by_use(tmp_path: Path):
    root = _repo(tmp_path)
    reqs = structured_requirements(_sdk_modules(), "mail_sdk.outbox.Outbox.send")
    helpers = repo_producers(root, reqs, "send", exclude=("app/legacy.py", 1, 2))
    listed = [item["helper"].split(" ", 1)[1].split("(", 1)[0] for item in helpers]
    # Producers by return type first (the list one, then the class one), then the
    # unannotated one the repo already passes as recipients=; a same-name helper
    # returning str, a single Recipient (not a list), and test helpers are left out.
    assert listed == ["as_recipients", "Clock.window", "gather"]
    assert helpers[0]["import"] == "from app.addresses import as_recipients"
    assert helpers[0]["for"] == "recipients: list[Recipient]"
    assert helpers[1]["note"] == "instance method of Clock"
    assert helpers[0]["call"] == "as_recipients(...)"
    # The file being fixed has its own as_recipients returning str: the listing says so.
    assert helpers[0]["caution"] == (
        "the code being fixed has its own as_recipients -> str, which is a different function and does not "
        "produce this type; call as_recipients(...) instead"
    )
    assert "caution" not in helpers[2]


def test_static_helper_is_called_through_its_class(tmp_path: Path):
    root = _write(
        tmp_path,
        {
            "app/book.py": """
                class Book:
                    @staticmethod
                    def entries(rows) -> "list[Recipient]":
                        return []
                """,
            "app/sender.py": """
                class Sender:
                    def entries(self, rows) -> str:
                        return ""

                    def run(self, outbox, rows):
                        return outbox.send(to=self.entries(rows))
                """,
        },
    )
    reqs = structured_requirements(_sdk_modules(), "mail_sdk.outbox.Outbox.send")[:1]
    helpers = repo_producers(root, reqs, "send", exclude=("app/sender.py", 5, 6))
    assert [item["call"] for item in helpers] == ["Book.entries(...)"]
    assert "Sender.entries -> str" in helpers[0]["caution"]


def test_nothing_structured_means_no_search(tmp_path: Path):
    assert repo_producers(_repo(tmp_path), [], "send") == []


def test_first_attempt_lists_repo_helpers(tmp_path: Path, monkeypatch):
    fixtures = tmp_path / "fixtures"
    _write(fixtures / "new", _SDK)
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"example-sdk": {"wheels": {"2.0": "new"}}}}), encoding="utf-8"
    )
    root = _repo(tmp_path / "repo")
    (root / "mod.py").write_text("\n" * 5 + "def broken(client):\n    return client.old_call(1)\n", encoding="utf-8")
    payload = _impact_payload()
    payload["impacts"][0]["symbol"] = "mail_sdk.Outbox.send"
    monkeypatch.setattr(verify_mod, "_breaks_listed", _hits_in_span)
    client = FakeModelClient(responses=[_good_edit(), "why"])
    apply_sites(
        sites_from_payload(payload),
        ApplyConfig(root=root, settings=_settings(), client=client, recheck_impact=True, fixtures=fixtures),
    )
    first = _sent(client, 0)
    pack = json.loads(first.split("Context pack (JSON):", 1)[1].rsplit("Return JSON", 1)[0])
    helpers = pack["release_diff"]["existing_repo_helpers_that_produce_this_type"]
    assert [item["helper"].split(" ", 1)[0] for item in helpers] == ["app/addresses.py:4", "app/digest.py:15", "app/digest.py:4"]
