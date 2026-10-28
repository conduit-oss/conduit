"""Dict keys a repo callable reads, against the dict a rewrite hands it.

The made-up ``orchard`` repo has a ``Tally`` whose class methods read a mapping
(``values["apples"]`` is needed, ``values.get("extra")`` is not, and one reads
through a helper) and a plain function that checks ``"bonus" in row`` first.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.apply.change_why import caught_keys
from conduit.apply.client import FakeModelClient
from conduit.apply.declared import declared_type_breaks
from conduit.apply.engine import ApplyConfig, _MISMATCH, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus

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
