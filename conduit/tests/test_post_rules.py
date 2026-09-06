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


def test_infer_string_rewrite_on_legacy_response_access(tmp_path: Path):
    from conduit.patcher.post_rules.mechanical import infer_from_repo_scan

    rel = "web/llm.py"
    path = tmp_path / "web"
    path.mkdir()
    (path / "llm.py").write_text(
        "import openai\n"
        "resp = openai.chat.completions.create(model='gpt-4o')\n"
        "text = resp['choices'][0]['message']['content']\n",
        encoding="utf-8",
    )
    rules = infer_from_repo_scan(tmp_path, [rel], packet={"package": "openai"})
    rewrites = [r for r in rules if r.get("type") == "STRING_REWRITE"]
    assert rewrites
    assert rewrites[0]["target_files"] == ["web/llm.py"]
    assert ".choices[0].message.content" in rewrites[0]["replace"]


def test_apply_string_rewrite_on_edited_file(tmp_path: Path):
    path = tmp_path / "llm.py"
    path.write_text(
        "resp = openai.chat.completions.create()\n"
        "return resp['choices'][0]['message']['content']\n",
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
                "type": "STRING_REWRITE",
                "target_files": ["llm.py"],
                "match": "['choices'][0]['message']['content']",
                "replace": ".choices[0].message.content",
            }
        ],
    }
    report = apply_post_rules(tmp_path, packet, vendor=False, learned=False)
    assert report.files_modified
    text = path.read_text(encoding="utf-8")
    assert "['choices']" not in text
    assert ".choices[0].message.content" in text


def test_synthesize_post_rules_llm_stamps_targets_before_validate(monkeypatch):
    from conduit.patcher.post_rules.llm import synthesize_post_rules_llm

    class FakeLLM:
        def complete_json(self, *, system: str, user: str):
            return {
                "post_rules": [
                    {
                        "type": "WRAPPER_ENSURE_LIST",
                        "function_name": "list_files",
                    }
                ]
            }

    monkeypatch.setattr("conduit.patcher.post_rules.llm.get_llm_client", lambda: FakeLLM())
    rules = synthesize_post_rules_llm(
        packet={"package": "openai", "rules": []},
        failure_digest="list_files failed",
        file_windows=[{"path": "files.py", "text": "def list_files(): pass"}],
        allowlist=["files.py", "other.py"],
        log=lambda _m: None,
    )
    assert rules
    assert rules[0]["target_files"] == ["files.py", "other.py"]
