"""A fixed line's reason says what changed, read from the code and the checks.

Made-up libraries only: a parcel SDK whose ``Reply.weight`` is the SDK's own
``Weight``, and a shop repo with a different class also called ``Weight``.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from conduit.apply import engine as engine_mod
from conduit.apply.change_why import caught, change_why
from conduit.apply.client import ChatMessage, FakeModelClient, TokenFactoryClient
from conduit.apply.engine import ApplyConfig, ApplyReport, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.report import model_call_lines

from test_apply import _impact_payload, _settings
from test_declared_types import _findings

_BODY = """
    def build(reply: Reply) -> Record:
        return Record(weight=reply.weight)
    """

_ORIGINAL = """\
def pack(reply: OldReply) -> Record:
    return Record(weight=reply.old_weight)
"""

_FIXED = """\
def pack(reply: Reply) -> Record:
    return Record(weight=Weight(grams=int(reply.weight.kilos * 1000), tare_grams=int(reply.weight.tare_kilos * 1000)))
"""


def _finding(tmp_path: Path, path: str = "shop/use.py") -> str:
    text = _findings(tmp_path, _BODY)[0]
    return text.replace("shop/use.py:", f"{path}:", 1)


def test_the_reason_names_both_classes_by_module_and_the_mapped_fields(tmp_path: Path):
    why = change_why(_ORIGINAL, _FIXED, 2, (1, 2), "OldReply", "types.OldReply removed", [_finding(tmp_path)])
    assert why.startswith("fixed `OldReply` (types.OldReply removed) in `pack`: ")
    assert "`reply` is now typed `Reply` instead of `OldReply`" in why
    assert "the type check caught `reply.weight` (`parcel_sdk.types.weight.Weight`) passed as `weight=`" in why
    assert "where `Record` in this repo expects `shop.records.Weight`" in why
    assert "builds `Weight` from the value's fields (grams <- `int(reply.weight.kilos * 1000)`" in why
    assert "tare_grams <- `int(reply.weight.tare_kilos * 1000)`" in why
    assert "stops reading `reply.old_weight`" in why
    assert "for the new release" not in why
    assert "\n" not in why


def test_without_a_finding_the_reason_still_reads_from_the_two_sources():
    why = change_why(_ORIGINAL, _FIXED, 2, (1, 2), "OldReply", "types.OldReply removed", [])
    assert "type check" not in why
    assert "`reply` is now typed `Reply` instead of `OldReply`" in why
    assert "builds `Weight` from the value's fields (grams <- " in why


def test_nothing_readable_gives_no_reason():
    assert change_why("x = 1\n", "x = 2\n", 1, (1, 1)) == ""
    same = "def f(a: int) -> int:\n    return a\n"
    assert change_why(same, same, 2, (1, 2)) == ""
    assert caught(["mod.py:3 has no member", ""]) == []


_REPO = """\
from parcel_sdk.types import Reply
from shop.records import Record, Weight


def broken(client, reply: Reply):
    return client.old_call(reply)
"""


def test_a_fix_the_type_check_shaped_gets_a_reason_from_the_finding(tmp_path: Path, monkeypatch):
    text = _finding(tmp_path / "shop-repo", "mod.py")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mod.py").write_text(_REPO, encoding="utf-8")
    line = _REPO.splitlines().index("    return client.old_call(reply)") + 1

    def breaks(_root, _site, patched, **_kwargs):
        return [(text, "")] if "Record(weight=reply.weight)" in patched else []

    monkeypatch.setattr(engine_mod, "remaining_breaks", breaks)
    first = "def broken(client, reply: Reply):\n    return client.new_call(Record(weight=reply.weight))\n"
    second = (
        "def broken(client, reply: Reply):\n"
        "    return client.new_call(Record(weight=Weight(grams=int(reply.weight.kilos * 1000))))\n"
    )
    edits = [json.dumps({"function_source": body, "new_imports": []}) for body in (first, second)]
    client = FakeModelClient(responses=[*edits, "Updated the call."])
    report = apply_sites(
        sites_from_payload(_impact_payload(line=line)),
        ApplyConfig(root=root, settings=_settings(max_attempts=3), client=client, recheck_impact=True),
    )
    result = report.results[0]
    assert result.status == SiteStatus.FIXED_UNVERIFIED
    assert result.attempts == 2
    assert "the type check caught `reply.weight` (`parcel_sdk.types.weight.Weight`)" in result.why
    assert "expects `shop.records.Weight`" in result.why
    assert "builds `Weight` from the value's fields (grams <- `int(reply.weight.kilos * 1000)`)" in result.why
    assert "for the new release" not in result.why


def test_model_calls_are_summarized_for_the_log_without_the_key():
    secret = "sk-test-never-shown"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    client = TokenFactoryClient(base_url="https://user:pw@models.example.test/v1/", api_key=secret)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    hello = [ChatMessage(role="user", content="hi")]
    client.chat(model="big/model", turns=hello)
    client.chat(model="big/model", turns=hello)
    client.chat(model="small/model", turns=hello, enable_thinking=True)
    report = ApplyReport(tokens_used=45, model_calls=client.call_summary())
    lines = model_call_lines(report)
    assert lines == [
        "Token Factory call: POST https://models.example.test/v1/chat/completions model=big/model thinking=off calls=2 tokens=30",
        "Token Factory call: POST https://models.example.test/v1/chat/completions model=small/model thinking=on calls=1 tokens=15",
        "Token Factory total: calls=3 tokens=45",
    ]
    assert all(secret not in line and "pw" not in line for line in lines)
    assert report.to_dict()["model_calls"][0]["model"] == "big/model"
    assert model_call_lines(ApplyReport()) == []
