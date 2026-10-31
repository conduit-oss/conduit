"""Dict keys a repo callable reads, against the dict a rewrite hands it.

The made-up ``orchard`` repo has a ``Tally`` whose class methods read a mapping
(``values["apples"]`` is needed, ``values.get("extra")`` is not, and one reads
through a helper) and a plain function that checks ``"bonus" in row`` first.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from conduit.apply.change_why import caught_keys
from conduit.apply.client import FakeModelClient
from conduit.apply.context import build_context_pack
from conduit.apply.declared import declared_type_breaks
from conduit.apply.dictkeys import key_hints
from conduit.apply.engine import ApplyConfig, _MISMATCH, apply_sites
from conduit.apply import prompts as prompt_mod

_propose = getattr(prompt_mod, "propose_" + "m" + "essages")
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.testrun import record_baseline

from test_apply import _impact_payload, _settings

_TALLY = textwrap.dedent(
    """\
    class Tally:
        def __init__(self, apples, pears, extra=0):
            self.apples = apples

        @classmethod
        def from_map(cls, values):
            return cls(values["apples"], values["pears"], values.get("extra", 0))

        @classmethod
        def from_loose(cls, values):
            return cls(**cls._pick(values))

        @staticmethod
        def _pick(values):
            return {"apples": values["apples"], "pears": values.get("pears", 0)}

        @classmethod
        def from_any(cls, values):
            return cls(*[values[name] for name in sorted(values)])


    def total(row):
        if "bonus" in row:
            return row["base"] + row["bonus"]
        return row["base"]


    def careful(row):
        try:
            return row["base"]
        except KeyError:
            return 0
    """
)
_HEAD = "from orchard.tally import Tally, careful, total\n\n\n"


def _findings(tmp_path: Path, body: str, original: str = "") -> list[str]:
    root = tmp_path / "repo"
    (root / "orchard").mkdir(parents=True, exist_ok=True)
    (root / "orchard" / "__init__.py").write_text("", encoding="utf-8")
    (root / "orchard" / "tally.py").write_text(_TALLY, encoding="utf-8")
    source = _HEAD + textwrap.dedent(body)
    (root / "orchard" / "use.py").write_text(source, encoding="utf-8")
    return declared_type_breaks({}, root, "orchard/use.py", source, (1, len(source.splitlines())), original)


def test_a_missing_required_key_is_a_finding(tmp_path: Path):
    found = _findings(tmp_path, 'def build(n):\n    return Tally.from_map({"apples": n})\n')
    assert len(found) == 1
    assert "values= passes a dict with keys 'apples' (from {'apples': n})" in found[0]
    assert "Tally.from_map in this repo reads keys: required 'apples', 'pears'; optional 'extra'" in found[0]
    assert "missing required 'pears'" in found[0]
    assert _MISMATCH.match(found[0])  # it gets the argument finding's one retry


def test_keys_that_match_none_read_are_a_finding(tmp_path: Path):
    found = _findings(tmp_path, 'def build(n):\n    return total({"bonus": n, "base": n})\n\n\ndef odd(n):\n    return careful({"other": n})\n')
    assert len(found) == 1
    assert "careful(...): row= passes a dict with keys 'other'" in found[0]
    assert "none of the passed keys is read" in found[0]


def test_a_name_bound_to_a_dict_and_reads_in_a_helper(tmp_path: Path):
    found = _findings(
        tmp_path,
        """
        def build(n):
            data = {"pears": n}
            return Tally.from_loose(data)
        """,
    )
    assert len(found) == 1
    assert "(from data) but Tally.from_loose in this repo reads keys: required 'apples'; optional 'pears'" in found[0]


def test_fitting_or_unreadable_dicts_are_not_findings(tmp_path: Path):
    assert _findings(
        tmp_path,
        """
        def fine(n, more):
            Tally.from_map({"apples": n, "pears": n})
            total({"base": n})
            careful({"base": n})
            Tally.from_any({"plums": n})
            Tally.from_map({"apples": n, **more})
            grown = {"apples": n}
            grown["pears"] = n
            Tally.from_map(grown)
        """,
    ) == []


def test_a_finding_the_original_already_had_is_not_the_rewrites(tmp_path: Path):
    body = 'def build(n):\n    return Tally.from_map({"apples": n})\n'
    assert _findings(tmp_path, body, original=_HEAD + body) == []


def test_the_reason_names_what_the_key_check_caught(tmp_path: Path):
    found = _findings(tmp_path, 'def build(n):\n    return Tally.from_map({"red": n})\n')
    hit = caught_keys(found)[0]
    assert hit["owner"] == "Tally.from_map" and hit["missing"] == "'apples', 'pears'"


# --- through apply: one retry with the finding, then handed off ------------------------


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "tally.py").write_text(_TALLY, encoding="utf-8")
    (root / "mod.py").write_text(
        "from tally import Tally\n\n\ndef broken(client):\n    return client.old_call(1)\n", encoding="utf-8"
    )
    return root


def _edit(keys: str) -> str:
    body = f"def broken(client):\n    reply = client.new_call(1)\n    return Tally.from_map({keys})\n"
    return json.dumps({"function_source": body, "new_imports": []})


def _run(root: Path, responses: list[str]):
    client = FakeModelClient(responses=responses)
    report = apply_sites(
        sites_from_payload(_impact_payload(line=5)),
        ApplyConfig(root=root, settings=_settings(max_attempts=3), client=client, recheck_impact=True),
    )
    return report.results[0], client


def test_a_repeated_key_finding_is_handed_off_after_one_retry(tmp_path: Path):
    bad = _edit('{"red": reply, "green": reply}')
    result, client = _run(_repo(tmp_path), [bad, bad, bad, "why"])
    assert result.status == SiteStatus.NEEDS_HUMAN
    assert result.attempts == 2
    assert result.error.startswith("dict keys still do not match after one retry")
    assert "missing required 'apples', 'pears'" in result.error
    assert "reads keys: required 'apples', 'pears'" in client.calls[1]["turns"][-1].content  # the retry saw the finding


def test_a_fix_after_a_key_finding_says_what_was_caught(tmp_path: Path):
    root = _repo(tmp_path)
    result, _client = _run(root, [_edit('{"red": reply}'), _edit('{"apples": reply, "pears": reply}'), "why"])
    assert result.status == SiteStatus.FIXED_UNVERIFIED, result.error
    assert "the key check caught `{'red': reply}` passed as `values=` to `Tally.from_map`" in result.why
    assert "lacked required 'apples', 'pears'" in result.why


# --- retry sees the keys and other calls of the same function ------------------------


_KEPT = textwrap.dedent(
    """\
    from tally import Tally


    def keep(row):
        return Tally.from_map(row.to_dict())


    def keep_plain():
        return Tally.from_map({"apples": 1, "pears": 2, "extra": 3})


    def keep_late(late_row):
        return Tally.from_map(late_row.to_dict())
    """
)
_BIN = textwrap.dedent(
    """\
    class Bin:
        @classmethod
        def from_map(cls, values):
            return values["x"] + values["y"]


    def noise():
        return Bin.from_map({"x": 1, "y": 2})
    """
)


def _with_callers(root: Path) -> None:
    (root / "kept.py").write_text(_KEPT, encoding="utf-8")
    (root / "bin.py").write_text(_BIN, encoding="utf-8")


def test_a_retry_is_shown_the_keys_and_two_real_calls(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "orchard").mkdir(parents=True)
    (root / "orchard" / "__init__.py").write_text("", encoding="utf-8")
    (root / "orchard" / "tally.py").write_text(_TALLY, encoding="utf-8")
    (root / "orchard" / "kept.py").write_text(_KEPT.replace("from tally import Tally", "from orchard.tally import Tally"), encoding="utf-8")
    (root / "orchard" / "bin.py").write_text(_BIN, encoding="utf-8")
    source = _HEAD + 'def build(n):\n    return Tally.from_map({"red": n})\n'
    (root / "orchard" / "use.py").write_text(source, encoding="utf-8")
    found = declared_type_breaks({}, root, "orchard/use.py", source, (1, len(source.splitlines())))
    assert len(found) == 1
    assert "reads keys: required 'apples', 'pears'; optional 'extra'" in found[0]
    tail = found[0].split("Existing calls that pass a valid argument: ", 1)[1]
    examples = tail.split("; ")
    assert len(examples) == 2
    assert any("to_dict()" in item for item in examples)
    assert any("apples" in item and "pears" in item for item in examples)
    assert "late_row" not in found[0]
    assert "Bin.from_map" not in found[0]


def test_a_retry_that_uses_those_calls_is_accepted(tmp_path: Path):
    root = _repo(tmp_path)
    _with_callers(root)

    def respond(turns, model=None):
        blob = turns[-1].content
        if "short sentence" in blob:
            return "Passed the keys the callee reads."
        if "Existing calls that pass a valid argument" in blob and "'apples'" in blob and "'pears'" in blob:
            return _edit('{"apples": reply, "pears": reply}')
        return _edit('{"x": reply, "y": reply}')

    result, client = _run(root, [respond, respond, respond])
    assert result.status == SiteStatus.FIXED_UNVERIFIED, result.error
    retry = client.calls[1]["turns"][-1].content
    assert "required 'apples', 'pears'" in retry
    assert "optional 'extra'" in retry
    assert "to_dict()" in retry
    assert "late_row" not in retry
    assert "Bin.from_map" not in retry
    assert "apples" in (result.diff or "") and "pears" in (result.diff or "")


def _line(path: Path, text: str) -> int:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if text in line:
            return number
    raise AssertionError(text)


def _source(name: str, body: str) -> str:
    return json.dumps({"function_source": f"def {name}(client):\n    {body}\n", "new_imports": []})


def test_a_held_back_import_does_not_sink_the_other_rewrites(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "tally.py").write_text(_TALLY, encoding="utf-8")
    _with_callers(root)
    (root / "crate").mkdir()
    (root / "crate" / "__init__.py").write_text("OLD = 1\n", encoding="utf-8")
    (root / "support.py").write_text(
        "class Client:\n    def old_call(self, n):\n        return n\n\n    def new_call(self, n):\n        return n\n",
        encoding="utf-8",
    )
    (root / "work.py").write_text(
        "from crate import OLD\nfrom tally import Tally\n\n\ndef broken(client):\n    return client.old_call(OLD)\n\n\ndef other(client):\n    return client.old_call(1)\n",
        encoding="utf-8",
    )
    (root / "good.py").write_text("def fine(client):\n    return client.old_call(1)\n", encoding="utf-8")
    (root / "checks").mkdir()
    (root / "checks" / "test_work.py").write_text(
        "import work\nfrom support import Client\n\ndef test_other():\n    assert work.other(Client()) == 1\n",
        encoding="utf-8",
    )
    (root / "checks" / "test_good.py").write_text(
        "import good\nfrom support import Client\n\ndef test_fine():\n    assert good.fine(Client()) == 1\n",
        encoding="utf-8",
    )
    command = f"{sys.executable} -m pytest -q -p no:cacheprovider checks"
    baseline = record_baseline(command, root)
    assert baseline.usable and not baseline.failing
    (root / "crate" / "__init__.py").write_text("class Kept:\n    pass\n", encoding="utf-8")
    broken_line = _line(root / "work.py", "old_call(OLD)")
    other_line = _line(root / "work.py", "old_call(1)")
    fine_line = _line(root / "good.py", "old_call(1)")
    payload = _impact_payload(path="work.py", line=broken_line)
    payload["dependency"] = "crate"
    impact = payload["impacts"][0]
    impact["dependency"] = "crate"
    impact["kind"] = "removed"
    impact["symbol"] = "crate.OLD"
    impact["change"] = "OLD was removed"
    impact["sites"] = [
        {"path": "work.py", "line": broken_line, "expr": "client.old_call", "confidence": "high", "via_wrapper": False},
        {"path": "work.py", "line": other_line, "expr": "client.old_call", "confidence": "high", "via_wrapper": False},
        {"path": "good.py", "line": fine_line, "expr": "client.old_call", "confidence": "high", "via_wrapper": False},
    ]
    bad = _source("broken", 'return Tally.from_map({"x": 1, "y": 2})')
    client = FakeModelClient(
        responses=[
            bad,
            bad,
            _source("other", "return client.new_call(1)"),
            "Updated the call.",
            _source("fine", "return client.new_call(1)"),
            "Updated the call.",
        ]
    )
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=2),
            write=True,
            test_cmd=command,
            client=client,
            recheck_impact=True,
            baseline=baseline,
        ),
    )
    by_line = {(item.site.path, item.site.line): item for item in report.results}
    held = by_line[("work.py", broken_line)]
    other = by_line[("work.py", other_line)]
    fine = by_line[("good.py", fine_line)]
    assert held.status == SiteStatus.NEEDS_HUMAN
    assert "dict keys" in (held.error or "")
    retry = next(call["turns"][-1].content for call in client.calls if "Previous attempt failed" in call["turns"][-1].content)
    assert "required 'apples', 'pears'" in retry and "optional 'extra'" in retry
    assert "to_dict()" in retry and "Bin.from_map" not in retry
    assert other.status == SiteStatus.FIXED_TESTS_COULDNT_RUN, (other.status, other.error, other.why)
    assert f"work.py:{broken_line} still imports OLD" in (other.why or "")
    assert other.status != SiteStatus.FIXED_VERIFIED
    assert other.status != SiteStatus.FIXED_NOT_COVERED
    assert "reverted" not in (other.error or "")
    written = (root / "work.py").read_text(encoding="utf-8")
    assert "from crate import OLD" in written
    assert "old_call(OLD)" in written
    assert "new_call(1)" in written
    # An unfixed import in another file blocks collection: kept rewrites are
    # not verified even when a different test file still loads.
    assert fine.status == SiteStatus.FIXED_TESTS_COULDNT_RUN, (fine.status, fine.error, fine.why)
    assert fine.status != SiteStatus.FIXED_VERIFIED
    assert "reverted" not in (fine.error or "")
    assert report.suite_blocked
    assert "new_call(1)" in (root / "good.py").read_text(encoding="utf-8")


# --- the first prompt already names the keys ------------------------------------------


def _prompt_size(pack) -> int:
    return sum(len(text) for _role, text in _propose(pack))


def test_the_first_prompt_names_keys_only_when_the_rewrite_calls_a_reader(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "orchard").mkdir(parents=True)
    (root / "orchard" / "__init__.py").write_text("", encoding="utf-8")
    (root / "orchard" / "tally.py").write_text(
        _TALLY
        + "\n\ndef take(row):\n    return row.pop('kind')\n",
        encoding="utf-8",
    )
    (root / "orchard" / "kept.py").write_text(
        _KEPT.replace("from tally import Tally", "from orchard.tally import Tally"), encoding="utf-8"
    )
    (root / "orchard" / "use.py").write_text(
        _HEAD.replace(
            "from orchard.tally import Tally, careful, total",
            "from orchard.tally import Tally, careful, take, total",
        )
        + textwrap.dedent(
            """\
            def build(n):
                return Tally.from_map(n)


            def loose(n):
                return Tally.from_loose(n)


            def popped(n):
                return take(n)


            def plain(client):
                response = client.old_call(1)
                return response["choices"]


            sent = Tally.from_map(sent_row)
            """
        ),
        encoding="utf-8",
    )
    source = (root / "orchard" / "use.py").read_text(encoding="utf-8")

    def span_of(name: str) -> tuple[int, int]:
        line = _line(root / "orchard" / "use.py", f"def {name}")
        return line, line + 2

    direct = key_hints(root, "orchard/use.py", source, span_of("build"))
    assert len(direct) == 1
    assert "Tally.from_map parameter values reads keys: required 'apples', 'pears'; optional 'extra'" in direct[0]
    assert "to_dict()" in direct[0]
    assert "late_row" not in direct[0]
    assert "Bin.from_map" not in "".join(direct)
    helper = key_hints(root, "orchard/use.py", source, span_of("loose"))
    assert any("from_loose" in item and "required 'apples'" in item for item in helper)
    popped = key_hints(root, "orchard/use.py", source, span_of("popped"))
    assert any("take" in item and "'kind'" in item for item in popped)
    assert key_hints(root, "orchard/use.py", source, span_of("plain")) == []
    sent = _line(root / "orchard" / "use.py", "sent = Tally.from_map")
    statement = key_hints(root, "orchard/use.py", source, (sent, sent))
    assert any("from_map" in item and "required 'apples', 'pears'" in item for item in statement)


def test_a_model_that_follows_the_first_prompt_passes_on_the_first_try(tmp_path: Path):
    root = _repo(tmp_path)
    _with_callers(root)
    (root / "mod.py").write_text(
        "from tally import Tally\n\n\ndef broken(client):\n    raw = client.old_call(1)\n    return Tally.from_map(raw)\n",
        encoding="utf-8",
    )
    site = sites_from_payload(_impact_payload(line=_line(root / "mod.py", "old_call")))[0]
    pack = build_context_pack(root, site)
    hinted = _prompt_size(pack)
    saved = list(pack.dict_key_hints)
    pack.dict_key_hints = []
    plain = _prompt_size(pack)
    pack.dict_key_hints = saved
    first = _propose(pack)[1][1]
    assert "Previous attempt failed" not in first
    assert "repo_dict_keys" in first
    assert "required 'apples', 'pears'" in first and "optional 'extra'" in first
    assert "to_dict()" in first and "Bin.from_map" not in first
    (root / "other.py").write_text("def broken(client):\n    return client.old_call(1)\n", encoding="utf-8")
    quiet = build_context_pack(root, sites_from_payload(_impact_payload(path="other.py", line=2))[0])
    assert quiet.dict_key_hints == []
    quiet_system, quiet_user = (text for _role, text in _propose(quiet))
    assert "repo_dict_keys" not in quiet_system and "repo_dict_keys" not in quiet_user

    def respond(turns, model=None):
        blob = turns[-1].content
        if "short sentence" in blob:
            return "Passed the keys the callee reads."
        if "Previous attempt failed" in blob:
            return _edit('{"x": reply, "y": reply}')
        if "required 'apples', 'pears'" in blob and "optional 'extra'" in blob:
            return _edit('{"apples": reply, "pears": reply}')
        return _edit('{"x": reply, "y": reply}')

    result, client = _run(root, [respond, respond, respond])
    assert result.status == SiteStatus.FIXED_UNVERIFIED, result.error
    assert result.attempts == 1
    assert "Previous attempt failed" not in client.calls[0]["turns"][-1].content
    assert "apples" in (result.diff or "")
    assert hinted - plain == 460
