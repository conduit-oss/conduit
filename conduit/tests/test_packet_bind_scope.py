"""Bind catalog packets to the client pin and scope rules to usage."""

from __future__ import annotations

from conduit.packet.bind import bind_packet_to_client
from conduit.packet.scope import collapse_replace_chains, filter_rules_to_source
from conduit.self_correct import reject_self_correct_write
from conduit.text_tokens import obfuscated_forbidden_tokens, reconstructed_literals


def test_bind_packet_stamps_floor_from_installed():
    packet = {
        "packet_id": "openai-pypi-3.3.0",
        "package": "openai",
        "from_version": "0",
        "to_version": "3.3.0",
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "openai",
                "from_version": "0",
                "to_version": "3.3.0",
            }
        ],
    }
    bound = bind_packet_to_client(packet, installed_version="1.109.1")
    assert packet["from_version"] == "0"
    assert bound["from_version"] == "1.109.1"
    assert bound["rules"][0]["from_version"] == "1.109.1"


def test_bind_packet_leaves_non_floor_alone():
    packet = {
        "from_version": "2.0.0",
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "from_version": "2.0.0",
                "to_version": "3.0.0",
            }
        ],
    }
    bound = bind_packet_to_client(packet, installed_version="1.109.1")
    assert bound["from_version"] == "2.0.0"
    assert bound["rules"][0]["from_version"] == "2.0.0"


def test_collapse_replace_chains_lands_on_final():
    rules = [
        {
            "type": "EXACT_STRING_REPLACE",
            "match": "text-ada-001",
            "replace": "gpt-3.5-turbo-instruct",
        },
        {
            "type": "EXACT_STRING_REPLACE",
            "match": "gpt-3.5-turbo-instruct",
            "replace": "gpt-5.6-terra",
        },
        {"type": "DEPENDENCY_BUMP", "package": "openai", "to_version": "3.3.0"},
    ]
    collapsed, n = collapse_replace_chains(rules)
    by_match = {
        r["match"]: r["replace"]
        for r in collapsed
        if r.get("type") == "EXACT_STRING_REPLACE"
    }
    assert n >= 1
    assert by_match["text-ada-001"] == "gpt-5.6-terra"
    assert by_match["gpt-3.5-turbo-instruct"] == "gpt-5.6-terra"


def test_filter_rules_to_source_keeps_used_models_only():
    rules = [
        {
            "type": "DEPENDENCY_BUMP",
            "package": "openai",
            "from_version": "1.0.0",
            "to_version": "3.3.0",
        },
        {
            "type": "EXACT_STRING_REPLACE",
            "match": "text-ada-001",
            "replace": "gpt-3.5-turbo-instruct",
        },
        {
            "type": "EXACT_STRING_REPLACE",
            "match": "gpt-3.5-turbo-instruct",
            "replace": "gpt-5.6-terra",
        },
        {
            "type": "EXACT_STRING_REPLACE",
            "match": "davinci",
            "replace": "gpt-3.5-turbo",
        },
        {
            "type": "AST_CALL_REWRITE",
            "old_callee": "ChatCompletion.create",
            "new_callee": "chat.completions.create",
        },
    ]
    source = {
        "model_ids": ["text-ada-001"],
        "api_patterns": ["ChatCompletion.create"],
        "usages": [],
    }
    result = filter_rules_to_source(rules, source)
    matches = {
        r.get("match") or r.get("old_callee")
        for r in result.rules
        if r.get("type") != "DEPENDENCY_BUMP"
    }
    assert "text-ada-001" in matches
    assert "davinci" not in matches
    assert "ChatCompletion.create" in matches
    ada = next(r for r in result.rules if r.get("match") == "text-ada-001")
    assert ada["replace"] == "gpt-5.6-terra"


def test_reconstruct_join_and_reject_obfuscation():
    text = 'LEGACY_ADA = "".join(["a", "da"])\n'
    assert "ada" in reconstructed_literals(text)
    hidden = obfuscated_forbidden_tokens(text, ["ada"])
    assert hidden == ["ada"]
    packet = {
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "ada",
                "replace": "babbage-002",
            }
        ]
    }
    reason = reject_self_correct_write(
        "packages/openai_text/completions.py",
        text,
        packet=packet,
    )
    assert reason is not None
    assert "obfuscat" in reason
    assert reject_self_correct_write(
        "tests/test_conduit_oracle.py",
        "x = 1\n",
        packet=packet,
    )
    assert reject_self_correct_write(
        "vendor/legacy/helpers.py",
        "x = 1\n",
        packet=packet,
    )
