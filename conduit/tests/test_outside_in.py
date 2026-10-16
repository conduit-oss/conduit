"""Outside-in map: wrappers, re-exports, aliases, and Watch/apply wiring."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.blast import blast_radius
from conduit.outside_in.report import format_report, service_map_to_dict

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "wrapped-openai-fixture"
PACKET_PATH = REPO / "examples" / "sample-packet" / "conduit-packet.json"


def _packet() -> dict:
    return json.loads(PACKET_PATH.read_text(encoding="utf-8"))


def _packet_with_synthetic_param_rename() -> dict:
    """Blast coverage for AST_PARAM_RENAME. The 0.28.1 → 1.0.0 packet has no such rule."""
    packet = _packet()
    packet["rules"] = [
        *packet["rules"],
        {
            "type": "AST_PARAM_RENAME",
            "target_files": ["*.py"],
            "function_target": "chat.completions.create",
            "old_param": "max_tokens",
            "new_param": "max_completion_tokens",
            "reason": "Synthetic rule for wrapper param propagation.",
        },
    ]
    return packet


def _map():
    return analyze_repo(FIXTURE, service="openai")


def _import_openai_lines(text: str) -> int:
    return sum(
        1
        for line in text.splitlines()
        if line.strip() == "import openai" or line.strip().startswith("import openai ")
    )


def _by_symbol(chains, symbol: str, expr_contains: str | None = None):
    found = [chain for chain in chains if chain.call_symbol == symbol]
    if expr_contains is not None:
        found = [chain for chain in found if expr_contains in chain.call_expr]
    return found


def test_function_wrapper_reaches_app_call_site():
    chains = _map().chains
    ask = _by_symbol(chains, "ask", "complete")
    assert ask, "app.ask -> llm.complete was not traced"
    chain = next(item for item in ask if "ChatCompletion.create" in item.sdk_method)
    assert chain.via_wrapper
    assert chain.confidence == "high"
    assert chain.call_rel == "app.py"
    assert any(hop.symbol == "complete" and hop.rel == "llm.py" for hop in chain.hops)
    assert "max_tokens" in chain.params
    assert any("choices" in field for field in chain.response_fields) or any(
        "choices" in field
        for item in chains
        if item.call_rel == "llm.py" and not item.via_wrapper
        for field in item.response_fields
    )
    direct = [
        item
        for item in chains
        if item.call_rel == "llm.py" and not item.via_wrapper and "ChatCompletion" in item.sdk_method
    ]
    assert direct
    assert any("choices" in field for field in direct[0].response_fields)


def test_multi_hop_main_through_ask():
    chains = _map().chains
    main = _by_symbol(chains, "main")
    assert main
    chain = main[0]
    assert chain.via_wrapper
    symbols = [hop.symbol for hop in chain.hops]
    assert symbols[0] == "main"
    assert "ask" in symbols
    assert "complete" in symbols
    assert "ChatCompletion.create" in chain.sdk_method


def test_class_method_wrapper():
    chains = _map().chains
    via_class = _by_symbol(chains, "ask_via_class")
    assert via_class
    chain = via_class[0]
    assert chain.via_wrapper
    assert any(hop.symbol == "Gateway.complete" for hop in chain.hops)
    assert "ChatCompletion.create" in chain.sdk_method
    gateway_fn = _by_symbol(chains, "ask_gateway")
    assert gateway_fn
    assert any(hop.symbol == "Gateway.complete" for hop in gateway_fn[0].hops)


def test_package_reexport_of_wrapper():
    chains = _map().chains
    run = _by_symbol(chains, "run", "complete_chat")
    assert run, "consumer.run -> pkg.complete_chat was not traced"
    chain = run[0]
    assert chain.call_rel == "consumer.py"
    assert chain.via_wrapper
    assert any(hop.rel == "pkg/impl.py" and hop.symbol == "complete_chat" for hop in chain.hops)
    assert "ChatCompletion.create" in chain.sdk_method


def test_reexported_chatcompletion_symbol_is_a_direct_call():
    chains = _map().chains
    legacy = _by_symbol(chains, "legacy")
    assert legacy
    chain = legacy[0]
    assert chain.call_rel == "direct_alias.py"
    assert not chain.via_wrapper
    assert chain.sdk_method.endswith("ChatCompletion.create")
    assert chain.confidence == "high"


def test_openai_alias_constructor_and_return_helper():
    service_map = _map()
    aliases = [
        entry
        for entry in service_map.entries
        if entry.rel == "alias_client.py" and entry.kind == "sdk_alias"
    ]
    assert aliases
    assert any("OpenAI" in entry.symbol for entry in aliases)

    direct = _by_symbol(service_map.chains, "ask_direct")
    assert direct
    assert any("chat.completions.create" in item.sdk_method for item in direct)
    assert any(item.confidence == "high" and "chat.completions.create" in item.sdk_method for item in direct)

    aliased = _by_symbol(service_map.chains, "ask_aliased")
    assert aliased
    create_chains = [item for item in aliased if "chat.completions.create" in item.sdk_method]
    assert create_chains
    assert create_chains[0].confidence == "medium"
    constructor = _by_symbol(service_map.chains, "build", "C")
    assert constructor
    assert constructor[0].response_fields == ()


def test_http_host_and_wrapper():
    chains = _map().chains
    http = _by_symbol(chains, "ask_http")
    assert http
    chain = http[0]
    assert chain.kind == "http_call"
    assert chain.via_wrapper
    assert "api.openai.com/v1/chat/completions" in chain.sdk_method
    assert any(hop.symbol == "post_chat" for hop in chain.hops)


def test_getattr_and_unrelated_create_are_not_sdk_calls():
    chains = _map().chains
    assert not any(
        chain.call_rel == "dynamic_gap.py" and "ChatCompletion" in chain.sdk_method
        for chain in chains
    )
    assert not any(chain.call_rel == "unrelated.py" for chain in chains)


def test_blast_names_method_and_forwarded_param():
    service_map = _map()
    blast = blast_radius(service_map, _packet_with_synthetic_param_rename())
    ask = next(
        item
        for item in blast.affected
        if item.chain.call_symbol == "ask" and item.chain.call_rel == "app.py"
    )
    kinds = {issue.kind for issue in ask.breaks}
    assert "method_renamed" in kinds
    assert "param_renamed" in kinds
    assert any(issue.old == "max_tokens" for issue in ask.breaks)

    main = next(item for item in blast.affected if item.chain.call_symbol == "main")
    main_kinds = {issue.kind for issue in main.breaks}
    assert "method_renamed" in main_kinds
    assert "param_renamed" in main_kinds

def test_response_key_break_when_packet_says_so():
    service_map = _map()
    packet = {
        "packet_id": "openai-response",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "KEY_RENAME",
                "target_files": ["*.py"],
                "old_key": "content",
                "new_key": "text",
            }
        ],
    }
    blast = blast_radius(service_map, packet)
    direct = next(
        item
        for item in blast.affected
        if item.chain.call_rel == "llm.py" and not item.chain.via_wrapper
    )
    assert any(issue.kind == "response_key_renamed" and issue.old == "content" for issue in direct.breaks)


def test_map_cli_prints_wrapped_blast_radius():
    result = CliRunner(env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120"}).invoke(
        app,
        [
            "map",
            "--path",
            str(FIXTURE),
            "--service",
            "openai",
            "--packet",
            str(PACKET_PATH),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "openai" in payload["services"]
    wrapped = [
        item
        for item in payload["blast"]["affected"]
        if item["via_wrapper"] and "ChatCompletion.create" in item["sdk_method"]
    ]
    rels = {item["rel"] for item in wrapped}
    assert "app.py" in rels
    assert "consumer.py" in rels
    assert any(item["breaks"] for item in wrapped)
    text = CliRunner(env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120"}).invoke(
        app,
        ["map", "--path", str(FIXTURE), "--service", "openai", "--markdown"],
    )
    assert text.exit_code == 0, text.output
    assert "app.py" in text.output
    assert "ChatCompletion.create" in text.output


def test_each_call_site_lists_a_method_rename_once():
    """Suffix-equivalent packet callees are one change in text, Markdown, and JSON."""
    service_map = _map()
    blast = blast_radius(service_map, _packet())
    qualified = "method renamed: openai.ChatCompletion.create -> openai.chat.completions.create"
    short = "method renamed: ChatCompletion.create -> openai.chat.completions.create"
    rest_qualified = (
        "REST call matches packet callee openai.ChatCompletion.create -> "
        "openai.chat.completions.create"
    )
    rest_short = (
        "REST call matches packet callee ChatCompletion.create -> "
        "openai.chat.completions.create"
    )

    for item in blast.affected:
        renames = [issue for issue in item.breaks if issue.kind == "method_renamed"]
        by_new: dict[str, list] = {}
        for issue in renames:
            by_new.setdefault(issue.new, []).append(issue)
        for group in by_new.values():
            assert len(group) == 1, item.chain.call_site
        olds = {issue.old for issue in renames}
        assert "ChatCompletion.create" not in olds or "openai.ChatCompletion.create" not in olds

    text = format_report(service_map, blast, markdown=False)
    markdown = format_report(service_map, blast, markdown=True)
    payload = service_map_to_dict(service_map, blast)
    for rendered in (text, markdown):
        assert rendered.count(qualified) == sum(
            1
            for item in blast.affected
            if any(
                issue.kind == "method_renamed" and issue.old == "openai.ChatCompletion.create"
                for issue in item.breaks
            )
            and item.chain.kind != "http_call"
        )
        assert short not in rendered
        assert rest_short not in rendered
        assert rest_qualified not in rendered
    json_breaks = [
        issue
        for item in payload["blast"]["affected"]
        for issue in item["breaks"]
        if issue["kind"] == "method_renamed"
    ]
    assert json_breaks
    assert all(issue["old"] != "ChatCompletion.create" for issue in json_breaks)
    assert any(issue["old"] == "openai.ChatCompletion.create" for issue in json_breaks)
    http = [
        issue
        for item in payload["blast"]["affected"]
        if item["kind"] == "http_call"
        for issue in item["breaks"]
        if issue["kind"] == "method_renamed"
    ]
    # A dotted callee is not a URL path, so it does not invent a REST match.
    assert http == []

    # A different destination is a different change and stays visible.
    packet = _packet()
    rules = []
    for rule in packet["rules"]:
        if rule.get("type") == "AST_CALL_REWRITE" and rule.get("old_callee") == "ChatCompletion.create":
            rule = {**rule, "new_callee": "client.chat.completions.create"}
        rules.append(rule)
    packet = {**packet, "rules": rules}
    split = blast_radius(service_map, packet)
    legacy = next(item for item in split.affected if item.chain.call_symbol == "legacy")
    destinations = {issue.new for issue in legacy.breaks if issue.kind == "method_renamed"}
    assert destinations == {
        "openai.chat.completions.create",
        "client.chat.completions.create",
    }


def test_inner_kwarg_breaks_every_outer_call_site():
    """A synthetic param rename inside a wrapper is a break for every caller on that chain."""
    service_map = _map()
    blast = blast_radius(service_map, _packet_with_synthetic_param_rename())
    expected = {
        ("llm.py", "complete"),
        ("gateway.py", "Gateway.complete"),
        ("pkg/impl.py", "complete_chat"),
        ("app.py", "ask"),
        ("app.py", "main"),
        ("app.py", "ask_via_class"),
        ("gateway.py", "ask_gateway"),
        ("consumer.py", "run"),
        ("direct_alias.py", "legacy"),
    }
    seen: set[tuple[str, str]] = set()
    for item in blast.affected:
        if "ChatCompletion.create" not in item.chain.sdk_method:
            continue
        key = (item.chain.call_rel, item.chain.call_symbol)
        seen.add(key)
        params = [
            issue
            for issue in item.breaks
            if issue.kind == "param_renamed" and issue.old == "max_tokens"
        ]
        assert len(params) == 1, key
        assert params[0].confidence == item.chain.confidence
        assert params[0].new == "max_completion_tokens"
    assert expected <= seen

    aliased = next(
        item
        for item in blast.affected
        if item.chain.call_symbol == "ask_aliased"
        and "chat.completions.create" in item.chain.sdk_method
    )
    aliased_param = next(issue for issue in aliased.breaks if issue.old == "max_tokens")
    assert aliased.chain.confidence == "medium"
    assert aliased_param.confidence == "medium"

    http = next(item for item in blast.affected if item.chain.call_symbol == "ask_http")
    assert http.chain.kind == "http_call"
    assert not any(issue.kind == "param_renamed" for issue in http.breaks)

    text = format_report(service_map, blast)
    markdown = format_report(service_map, blast, markdown=True)
    payload = service_map_to_dict(service_map, blast)
    for rendered in (text, markdown):
        assert "app.py:19  main" in rendered
        main_block = rendered.split("app.py:19  main", 1)[1].split("\n- ", 1)[0]
        assert "param renamed: max_tokens -> max_completion_tokens" in main_block
        assert "consumer.py:9  run" in rendered
    consumer = next(
        item
        for item in payload["blast"]["affected"]
        if item["rel"] == "consumer.py" and item["symbol"] == "run"
    )
    assert any(issue["old"] == "max_tokens" for issue in consumer["breaks"])
    assert consumer["confidence"] == "high"

