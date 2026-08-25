"""Tests for post-rule engine, store, and vendor packs."""

from __future__ import annotations

import json
from pathlib import Path

from conduit.integrity import dummy_except_findings
from conduit.patcher.post_rules.engine import apply_post_rules
from conduit.patcher.post_rules.store import (
    export_post_rules_to_packet,
    load_learned_post_rules,
    merge_post_rules,
    save_learned_post_rules,
)
from conduit.patcher.post_rules.vendor import load_vendor_post_rules
from conduit.test_runner import resolve_consumer_python


def test_resolve_consumer_python_prefers_venv(tmp_path: Path):
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    resolved = resolve_consumer_python(tmp_path)
    assert resolved == str(venv_py)


def test_vendor_pack_loads_openai_rules():
    packet = {"ecosystem": "pypi", "package": "openai"}
    rules = load_vendor_post_rules(packet)
    assert any(r.get("type") == "WRAPPER_ENSURE_LIST" for r in rules)


def test_apply_wrapper_ensure_list(tmp_path: Path):
    pkg = tmp_path / "packages" / "openai_text"
    pkg.mkdir(parents=True)
    (pkg / "files.py").write_text(
        "import openai\nfrom openai_text.client import configure, get_api_key\n\n"
        "def list_files():\n"
        "    if not get_api_key():\n"
        "        configure()\n"
        "    response = openai.files.list()\n"
        "    return response['data']\n",
        encoding="utf-8",
    )
    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0",
        "to_version": "1",
        "rules": [],
        "post_rules": [
            {
                "type": "WRAPPER_ENSURE_LIST",
                "target_files": ["**/files.py"],
                "function_name": "list_files",
                "list_callee_hint": "openai.files.list()",
            }
        ],
    }
    report = apply_post_rules(tmp_path, packet, vendor=False, learned=False)
    assert report.files_modified
    text = (pkg / "files.py").read_text(encoding="utf-8")
    assert "return list(data)" in text


def test_runtime_model_alias_module_passes_anticheat(tmp_path: Path):
    pkg = tmp_path / "packages" / "openai_text"
    pkg.mkdir(parents=True)
    (pkg / "completions.py").write_text(
        "def complete_prompt(prompt, model='text-davinci-003'):\n"
        "    try:\n"
        "        return openai\n"
        "    except Exception:\n"
        "        return openai\n",
        encoding="utf-8",
    )
    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0",
        "to_version": "1",
        "rules": [],
        "runtime_model_aliases": {"gpt-5.6-terra": "gpt-3.5-turbo-instruct"},
        "live_test": {
            "alias_prefixes": ["gpt-5.6-"],
            "default_completion_model": "gpt-5.6-terra",
        },
        "post_rules": [
            {
                "type": "RUNTIME_MODEL_ALIAS",
                "target_files": ["**/completions.py"],
                "function_name": "complete_prompt",
                "replace_module": True,
            }
        ],
    }
    apply_post_rules(tmp_path, packet, vendor=False, learned=False)
    text = (pkg / "completions.py").read_text(encoding="utf-8")
    assert not dummy_except_findings(text, "completions.py")
    assert "DEFAULT_COMPLETION_MODEL" in text
    assert "gpt-3.5-turbo-instruct" not in text


def test_learned_post_rules_store_roundtrip(tmp_path: Path):
    rules = [{"type": "WRAPPER_ENSURE_LIST", "target_files": ["a.py"], "function_name": "f"}]
    save_learned_post_rules(tmp_path, rules, failure_fingerprint="abc")
    loaded = load_learned_post_rules(tmp_path)
    assert loaded == rules


def test_export_post_rules_to_packet():
    packet = {"packet_id": "t", "post_rules": []}
    learned = [
        {
            "type": "WRAPPER_DELEGATE",
            "target_files": ["e.py"],
            "function_name": "complete_with_engine",
            "delegate_module": "openai_text.completions",
            "delegate_symbol": "complete_prompt",
        }
    ]
    merged = export_post_rules_to_packet(packet, learned)
    assert len(merged["post_rules"]) == 1


def test_merge_post_rules_dedupes():
    a = [{"type": "X", "target_files": ["a.py"], "function_name": "f"}]
    b = list(a)
    merged = merge_post_rules(a, b)
    assert len(merged) == 1
