"""Separate anti-cheat: packet/AST floor, official SDK, shims, forbidden writes."""

from __future__ import annotations

from pathlib import Path

from conduit.anticheat.rules import reject_write
from conduit.anticheat.findings import AnticheatFinding
from conduit.anticheat.scan import run_anticheat, run_anticheat_mechanical
from conduit.self_correct import reject_self_correct_write
from conduit.text_tokens import obfuscated_forbidden_tokens, reconstructed_literals


def _packet() -> dict:
    return {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "/v1/engines",
                "replace": "/v1/models",
            },
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "ChatCompletion.create",
                "new_callee": "chat.completions.create",
            },
        ],
    }


def _packet_with_param_rename() -> dict:
    pkt = _packet()
    pkt["rules"] = list(pkt["rules"]) + [
        {
            "type": "AST_CALL_REWRITE",
            "old_callee": "Completion.create",
            "new_callee": "chat.completions.create",
        },
        {
            "type": "AST_PARAM_RENAME",
            "old_param": "prompt",
            "new_param": "messages",
            "new_callee": "chat.completions.create",
        },
    ]
    return pkt


def test_echo_script_stub_finding():
    from conduit.anticheat.rules import echo_script_stub_finding

    msg = echo_script_stub_finding(
        "scripts/legacy_smoke.sh",
        "#!/bin/sh\necho '/v1/engines'\n",
    )
    assert msg is not None
    assert "echo-stub" in msg


def test_legacy_callee_still_present():
    from conduit.anticheat.rules import legacy_callee_still_present

    packet = {
        "package": "openai",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "Completion.create",
                "new_callee": "chat.completions.create",
            }
        ],
    }
    msg = legacy_callee_still_present(
        "import openai\nopenai.Completion.create()\n",
        "quality.py",
        packet,
    )
    assert msg is not None
    assert "Completion.create" in msg


def test_legacy_callee_ignores_comments_and_docstrings():
    from conduit.anticheat.rules import legacy_callee_still_present

    packet = {
        "package": "openai",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "ChatCompletion.create",
                "new_callee": "chat.completions.create",
            }
        ],
    }
    text = (
        '"""Was openai.ChatCompletion.create; now client.chat.completions.create."""\n'
        "# ChatCompletion.create migrated\n"
        "from openai import OpenAI\n"
        "OpenAI().chat.completions.create(model='gpt-4o', messages=[])\n"
    )
    assert legacy_callee_still_present(text, "client.py", packet) is None


def test_allow_dropped_import_when_using_local_client_helper():
    previous = "import openai\n\ndef chat():\n    return openai.ChatCompletion.create()\n"
    content = (
        "from openai_text.client import get_client\n"
        "\n"
        "def chat():\n"
        "    return get_client().chat.completions.create(model='gpt-4o', messages=[])\n"
    )
    assert (
        reject_write(
            "packages/openai_text/chat.py",
            content,
            packet=_packet(),
            previous=previous,
        )
        is None
    )


def test_reject_fake_response_client():
    previous = "import openai\n\ndef chat():\n    return openai.ChatCompletion.create()\n"
    content = (
        "import requests\n"
        "def call():\n"
        "    return requests.get('https://api.example/v1/models').json()\n"
    )
    reason = reject_write(
        "packages/openai_text/client.py",
        content,
        packet=_packet(),
        previous=previous,
    )
    assert reason is not None
    assert "dropped official" in reason or "HTTP paths" in reason


def test_allow_official_sdk_migration():
    previous = "import openai\nresp = openai.ChatCompletion.create(model='gpt-4')\n"
    content = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "resp = client.chat.completions.create(model='gpt-4o', messages=[])\n"
    )
    assert (
        reject_write(
            "chat.py", content, packet=_packet(), previous=previous
        )
        is None
    )


def test_fstring_obfuscates_engines():
    text = "route = f\"/{'engines'}\"\n"
    assert "/engines" in reconstructed_literals(text) or any(
        "engines" in x for x in reconstructed_literals(text)
    )
    hidden = obfuscated_forbidden_tokens(text, ["/engines", "/v1/engines"])
    assert hidden


def test_byte_decode_obfuscates_legacy_model():
    text = (
        "def _decode(*codes: int) -> str:\n"
        "    return bytes(codes).decode()\n"
        "\n"
        "SNAP = _decode(103, 112, 116, 45, 52, 45, 51, 50, 107, 45, 48, 54, 49, 51)\n"
    )
    rebuilt = reconstructed_literals(text)
    assert "gpt-4-32k-0613" in rebuilt
    hidden = obfuscated_forbidden_tokens(text, ["gpt-4-32k-0613", "davinci"])
    assert "gpt-4-32k-0613" in hidden


def test_reject_packet_json_write():
    reason = reject_self_correct_write(
        "packets/openai-pypi-3.3.1.json",
        '{"packet_id": "x"}',
        packet=_packet(),
    )
    assert reason is not None
    assert "packet" in reason.lower() or "cannot edit" in reason


def test_reject_jsonl_seed_write():
    reason = reject_write(
        "services/knowledge_rag/data/faq_seed.jsonl",
        '{"text": "davinci"}\n',
        packet=_packet(),
        previous="",
    )
    assert reason is not None


def test_mechanical_scan_flags_parallel_http(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("openai==1.0.0\n", encoding="utf-8")
    (tmp_path / "client.py").write_text(
        "import requests\n"
        "def request_json(method, path):\n"
        "    if path.endswith('/v1/models'):\n"
        "        return requests.request(method, path).json()\n",
        encoding="utf-8",
    )
    report = run_anticheat_mechanical(tmp_path, _packet())
    assert report.failed
    assert any(
        "HTTP paths" in f or "official openai" in f.lower() for f in report.findings
    )


def test_llm_auditor_adds_findings(tmp_path: Path, monkeypatch):
    (tmp_path / "app.py").write_text("from openai import OpenAI\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("openai==1.0.0\n", encoding="utf-8")

    def _fake_audit(*_a, **_k):
        cheat = AnticheatFinding(
            path="app.py",
            kind="cheat",
            detail="dummy HTTP stub",
            severity="block",
            source="llm",
        )
        return [cheat], [], {
            "honesty": 40,
            "migration_completeness": 70,
            "notes": ["stub"],
        }

    monkeypatch.setattr(
        "conduit.anticheat.llm_audit.llm_audit_findings", _fake_audit
    )
    report = run_anticheat(tmp_path, _packet(), llm=True)
    assert report.failed
    assert any("dummy HTTP stub" in f for f in report.findings)
    assert report.score and report.score.get("honesty") == 40


def test_mechanical_finding_not_cleared_by_empty_auditor(tmp_path: Path, monkeypatch):
    (tmp_path / "client.py").write_text(
        "import openai\n"
        "def call():\n"
        "    try:\n"
        "        return openai.chat.completions.create(model='m', messages=[])\n"
        "    except ConnectionError:\n"
        "        return {'id': 'stub', 'choices': []}\n",
        encoding="utf-8",
    )

    def _clean(*_a, **_k):
        return [], [], {"honesty": 90, "migration_completeness": 80, "notes": []}

    monkeypatch.setattr(
        "conduit.anticheat.llm_audit.llm_audit_findings", _clean
    )
    report = run_anticheat(tmp_path, _packet(), llm=True)
    assert report.failed
    assert report.source in {"mechanical", "mixed"}
    assert report.score and report.score.get("honesty") == 90


def test_reject_sitecustomize_oracle_shim():
    content = (
        "from pathlib import Path\n"
        "_ORIGINAL_READ_TEXT = Path.read_text\n"
        "def _patched_read_text(self, *args, **kwargs):\n"
        "    text = _ORIGINAL_READ_TEXT(self, *args, **kwargs)\n"
        "    return text.replace('davinci', '__LEGACY__')\n"
        "Path.read_text = _patched_read_text\n"
        "# reads tests/test_conduit_oracle.py FORBIDDEN\n"
    )
    reason = reject_write(
        "sitecustomize.py", content, packet=_packet(), previous=""
    )
    assert reason is not None
    assert "read_text" in reason.lower() or "shim" in reason.lower() or "oracle" in reason.lower()


def test_reject_test_shim_install():
    content = (
        "from pathlib import Path\n"
        "_ORIGINAL_READ_TEXT = Path.read_text\n"
        "def install():\n"
        "    def _sanitize_text(text):\n"
        "        return text.replace('engines', '__LEGACY__')\n"
        "    def _patched_read_text(self, *a, **k):\n"
        "        return _sanitize_text(_ORIGINAL_READ_TEXT(self, *a, **k))\n"
        "    Path.read_text = _patched_read_text\n"
        "# FORBIDDEN from test_conduit_oracle\n"
    )
    reason = reject_write(
        "packages/openai_text/_test_shim.py",
        content,
        packet=_packet(),
        previous="",
    )
    assert reason is not None


def test_mechanical_scan_flags_sitecustomize(tmp_path: Path):
    (tmp_path / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "_ORIGINAL_READ_TEXT = Path.read_text\n"
        "def _patched_read_text(self, *args, **kwargs):\n"
        "    text = _ORIGINAL_READ_TEXT(self, *args, **kwargs)\n"
        "    return text.replace('x', '__LEGACY__')\n"
        "Path.read_text = _patched_read_text\n"
        "FORBIDDEN = True  # leftover-oracle launder\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "from openai import OpenAI\nclient = OpenAI()\n",
        encoding="utf-8",
    )
    report = run_anticheat_mechanical(tmp_path, _packet())
    assert report.failed
    assert any("read_text" in f.lower() or "shim" in f.lower() for f in report.findings)


def test_reject_compat_chat_monkeypatch():
    content = (
        "import openai\n"
        "class _CompatChatCompletions:\n"
        "    def create(self, *, prompt=None, messages=None, **kwargs):\n"
        "        return {'choices': [{'text': ''}]} \n"
        "class _CompatChatNamespace:\n"
        "    def __init__(self):\n"
        "        self.completions = _CompatChatCompletions()\n"
        "def _install_compat():\n"
        "    openai.chat = _CompatChatNamespace()\n"
    )
    reason = reject_write(
        "packages/openai_text/client.py",
        content,
        packet=_packet(),
        previous="import openai\n",
    )
    assert reason is not None
    assert (
        "compat" in reason.lower()
        or "monkeypatch" in reason.lower()
        or "openai.chat" in reason.lower()
    )


def test_reject_old_kwarg_via_param_rename():
    content = (
        "import openai\n"
        "def complete_prompt(prompt, model='gpt-4'):\n"
        "    return openai.chat.completions.create(\n"
        "        model=model, prompt=prompt, max_tokens=64\n"
        "    )\n"
    )
    reason = reject_write(
        "completions.py",
        content,
        packet=_packet_with_param_rename(),
        previous="import openai\n",
    )
    assert reason is not None
    assert "prompt=" in reason


def test_allow_messages_chat_create():
    content = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "client.chat.completions.create(\n"
        "    model='gpt-4o',\n"
        "    messages=[{'role': 'user', 'content': 'hi'}],\n"
        ")\n"
    )
    assert (
        reject_write(
            "chat.py",
            content,
            packet=_packet_with_param_rename(),
            previous="import openai\n",
        )
        is None
    )


def test_mock_patch_uses_packet_package():
    content = (
        "from unittest.mock import MagicMock, patch\n"
        "import stripe\n"
        "with patch('stripe.Charge.create', MagicMock()):\n"
        "    pass\n"
    )
    pkt = {
        "packet_id": "s",
        "package": "stripe",
        "ecosystem": "pypi",
        "rules": [],
    }
    reason = reject_write("tests/test_x.py", content, packet=pkt, previous="")
    assert reason is not None
    assert "stripe" in reason.lower()


def test_synthetic_except_without_fake_name():
    content = (
        "import openai\n"
        "def call():\n"
        "    try:\n"
        "        return openai.chat.completions.create(model='m', messages=[])\n"
        "    except ConnectionError:\n"
        "        return {'id': 'x', 'data': []}\n"
    )
    reason = reject_write(
        "client.py", content, packet=_packet(), previous="import openai\n"
    )
    assert reason is not None
    assert "synthetic" in reason.lower()


def test_synthetic_except_skips_non_sdk_fallback_in_same_file():
    from conduit.anticheat.rules import synthetic_except_findings

    content = (
        "import openai\n"
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def listennotes(url):\n"
        "    try:\n"
        "        return fetch(url)\n"
        "    except Exception:\n"
        "        return None\n"
        "def whisper(f):\n"
        "    try:\n"
        "        resp = client.audio.transcriptions.create(model='whisper-1', file=f)\n"
        "        return resp.text\n"
        "    except Exception as e:\n"
        "        return {'status': False, 'error': str(e)}\n"
    )
    assert synthetic_except_findings(content, "podcast_ingest.py", "openai") == []


def test_synthetic_except_skips_error_envelope_after_sdk_call():
    from conduit.anticheat.rules import synthetic_except_findings

    content = (
        "import openai\n"
        "def call():\n"
        "    try:\n"
        "        gpt_response = openai.chat.completions.create(model='m', messages=[])\n"
        "        return gpt_response.choices[0].message.content\n"
        "    except Exception as e:\n"
        "        return {'status': False, 'error': str(e)}\n"
    )
    assert synthetic_except_findings(content, "web/reNgine/llm.py", "openai") == []


def test_synthetic_except_skips_files_without_package():
    from conduit.anticheat.rules import synthetic_except_findings

    content = (
        "def lookup(port):\n"
        "    try:\n"
        "        return real(port)\n"
        "    except Exception:\n"
        "        return {'service_name': '', 'description': ''}\n"
    )
    assert synthetic_except_findings(content, "ports.py", "openai") == []


def test_synthetic_except_skips_preexisting_whisper_soft_fail():
    from conduit.anticheat.rules import synthetic_except_findings
    from conduit.integrity import dummy_except_findings

    previous = (
        "import openai\n"
        "def _transcribe_with_whisper(f):\n"
        "    try:\n"
        "        resp = openai.Audio.transcribe(model='whisper-1', file=f)\n"
        "        return (resp.get('text') or '').strip() or None\n"
        "    except Exception as e:\n"
        "        print(f'Whisper failed: {e}')\n"
        "        return None\n"
    )
    content = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def _transcribe_with_whisper(f):\n"
        "    try:\n"
        "        resp = client.audio.transcriptions.create(model='whisper-1', file=f)\n"
        "        return (resp.text or '').strip() or None\n"
        "    except Exception as e:\n"
        "        print(f'Whisper failed: {e}')\n"
        "        return None\n"
    )
    assert (
        synthetic_except_findings(
            content, "podcast_ingest.py", "openai", previous=previous
        )
        == []
    )
    assert (
        dummy_except_findings(
            content, "podcast_ingest.py", "openai", previous=previous
        )
        == []
    )


def test_synthetic_except_soft_fail_fail_closed_without_previous():
    from conduit.anticheat.rules import synthetic_except_findings

    content = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def whisper(f):\n"
        "    try:\n"
        "        resp = client.audio.transcriptions.create(model='whisper-1', file=f)\n"
        "        return resp.text\n"
        "    except Exception as e:\n"
        "        print(e)\n"
        "        return None\n"
    )
    hits = synthetic_except_findings(content, "podcast_ingest.py", "openai")
    assert hits and "synthetic" in hits[0].lower()


def test_synthetic_except_flags_new_soft_fail_not_in_previous():
    from conduit.anticheat.rules import synthetic_except_findings

    previous = "from openai import OpenAI\nclient = OpenAI()\n"
    content = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def whisper(f):\n"
        "    try:\n"
        "        resp = client.audio.transcriptions.create(model='whisper-1', file=f)\n"
        "        return resp.text\n"
        "    except Exception:\n"
        "        return None\n"
    )
    hits = synthetic_except_findings(
        content, "podcast_ingest.py", "openai", previous=previous
    )
    assert hits and "synthetic" in hits[0].lower()


def test_synthetic_except_flags_success_stub_even_if_in_previous():
    from conduit.anticheat.rules import synthetic_except_findings

    body = (
        "import openai\n"
        "def call():\n"
        "    try:\n"
        "        return openai.chat.completions.create(model='m', messages=[])\n"
        "    except Exception:\n"
        "        return {'choices': []}\n"
    )
    hits = synthetic_except_findings(
        body, "client.py", "openai", previous=body
    )
    assert hits and "synthetic" in hits[0].lower()


def test_anticheat_baseline_roundtrip(tmp_path: Path):
    from conduit.anticheat.baseline import (
        load_anticheat_baseline,
        save_anticheat_baseline,
    )

    (tmp_path / "app.py").write_text("import openai\nx = 1\n", encoding="utf-8")
    save_anticheat_baseline(tmp_path, ["app.py"])
    loaded = load_anticheat_baseline(tmp_path)
    assert loaded["app.py"] == "import openai\nx = 1\n"
    assert list(loaded) == ["app.py"]


def test_anticheat_baseline_normalizes_absolute_paths(tmp_path: Path):
    from conduit.anticheat.baseline import (
        load_anticheat_baseline,
        save_anticheat_baseline,
    )

    app = tmp_path / "podcast_ingest.py"
    app.write_text("import openai\nreturn None\n", encoding="utf-8")
    # Allowlist often contains absolute paths after expand_apply_allowlist.
    save_anticheat_baseline(tmp_path, [str(app.resolve())])
    loaded = load_anticheat_baseline(tmp_path)
    assert "podcast_ingest.py" in loaded
    assert not any(k.startswith("D:") or k.startswith("/") for k in loaded)

    # Legacy absolute keys on disk still load as relative.
    import json

    legacy = {
        str(app.resolve()).replace("\\", "/"): "import openai\nold\n",
    }
    path = tmp_path / ".conduit" / "anticheat_baseline.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded2 = load_anticheat_baseline(tmp_path)
    assert loaded2["podcast_ingest.py"] == "import openai\nold\n"


def test_format_failure_reasons_anticheat_and_pytest():
    from conduit.self_correct import format_failure_reasons
    from conduit.test_runner import TestResult

    anti = TestResult(
        runner="anticheat",
        passed=False,
        returncode=1,
        stdout=(
            "anticheat failed:\n"
            "podcast_ingest.py: synthetic_except — 514 except path returns a "
            "synthetic response without calling the official SDK\n"
            "podcast_ingest.py: mechanical — 514 swallows Exception and returns a dummy\n"
        ),
        stderr="",
        command=[],
    )
    reasons = format_failure_reasons(anti)
    assert len(reasons) == 2
    assert "synthetic_except" in reasons[0]
    assert "mechanical" in reasons[1]

    py = TestResult(
        runner="pytest",
        passed=False,
        returncode=1,
        stdout=(
            "FAILED tests/test_conduit_oracle.py::test_chat - assert 0\n"
            "FAILED tests/test_x.py::test_y - TypeError: boom\n"
            "E   AssertionError: expected client\n"
            "E   TypeError: boom\n"
        ),
        stderr="",
        command=[],
    )
    py_reasons = format_failure_reasons(py)
    assert any("test_conduit_oracle" in r for r in py_reasons)
    assert any("—" in r for r in py_reasons)


def test_mechanical_scan_respects_edited_files_only(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("openai==1.0.0\n", encoding="utf-8")
    (tmp_path / "untouched.py").write_text(
        "import openai\n"
        "def bad():\n"
        "    try:\n"
        "        return openai.chat.completions.create(model='m', messages=[])\n"
        "    except Exception:\n"
        "        return {}\n",
        encoding="utf-8",
    )
    (tmp_path / "edited.py").write_text(
        "from openai import OpenAI\n"
        "client = OpenAI()\n",
        encoding="utf-8",
    )
    full = run_anticheat_mechanical(tmp_path, _packet())
    assert full.failed
    scoped = run_anticheat_mechanical(tmp_path, _packet(), files=["edited.py"])
    assert not scoped.failed
    empty = run_anticheat_mechanical(tmp_path, _packet(), files=[])
    assert not empty.failed


def test_deny_substring_from_packet_only():
    bare = (
        "import openai\n"
        "def x():\n"
        "    return 'chatcmpl-fallback'\n"
    )
    assert (
        reject_write("a.py", bare, packet=_packet(), previous="import openai\n")
        is None
    )
    pkt = _packet()
    pkt["anticheat"] = {"deny_substrings": ["chatcmpl-fallback"]}
    reason = reject_write("a.py", bare, packet=pkt, previous="import openai\n")
    assert reason is not None
    assert "deny_substring" in reason


def test_banned_kwargs_on_hint():
    content = (
        "import openai\n"
        "openai.chat.completions.create(model='m', engine='e')\n"
    )
    pkt = _packet()
    pkt["anticheat"] = {
        "banned_kwargs_on": {"chat.completions.create": ["engine"]}
    }
    reason = reject_write(
        "c.py", content, packet=pkt, previous="import openai\n"
    )
    assert reason is not None
    assert "engine=" in reason


def test_js_join_obfuscation_detected():
    text = 'const legacy = ["a", "da"].join("");\n'
    rebuilt = reconstructed_literals(text, path="client.js")
    assert "ada" in rebuilt
    hidden = obfuscated_forbidden_tokens(text, ["ada"], path="client.js")
    assert hidden == ["ada"]


def test_minified_js_reconstruction_is_fast():
    import time

    # Quote-dense blob similar to vendor.min.js — must not hang.
    chunk = 'a="x";b=["y","z"];' * 8000
    text = chunk + 'var x=["a","da"].join("");' + chunk
    t0 = time.perf_counter()
    rebuilt = reconstructed_literals(text, path="vendor.min.js")
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"JS reconstruction too slow: {elapsed:.2f}s"
    assert "ada" in rebuilt


def test_large_python_join_still_detected_quickly():
    import time

    padding = 'x = "hello world"\n' * 2000
    text = padding + 'LEGACY = "".join(["a", "da"])\n' + padding
    t0 = time.perf_counter()
    rebuilt = reconstructed_literals(text, path="big.py")
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"Python reconstruction too slow: {elapsed:.2f}s"
    assert "ada" in rebuilt
    hidden = obfuscated_forbidden_tokens(text, ["ada"], path="big.py")
    assert hidden == ["ada"]


def test_mechanical_scan_emits_start_and_done(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "import openai\nopenai.chat.completions.create()\n", encoding="utf-8"
    )
    logs: list[str] = []
    run_anticheat_mechanical(
        tmp_path,
        _packet(),
        log=logs.append,
    )
    joined = "\n".join(logs)
    assert "[anticheat] mechanical scan:" in joined
    assert "[anticheat] mechanical done" in joined


def test_fake_client_skips_tests_and_substring_openai_mentions():
    from conduit.anticheat.rules import fake_client_findings, file_findings

    litellm_test = (
        "from unittest.mock import AsyncMock, patch\n"
        "import pytest\n\n"
        "async def test_gpt5_prefix():\n"
        "    with patch('pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion') as m:\n"
        "        m.return_value = AsyncMock()\n"
        "        kwargs = {'allowed_openai_params': ['reasoning_effort'], "
        "'model': 'openai/gpt-5'}\n"
        "        assert 'reasoning_effort' in kwargs['allowed_openai_params']\n"
    )
    assert (
        fake_client_findings(
            litellm_test,
            "tests/unittest/test_litellm_reasoning_effort.py",
            _packet(),
        )
        == []
    )
    assert (
        file_findings(
            "tests/unittest/test_litellm_reasoning_effort.py",
            litellm_test,
            _packet(),
        )
        == []
    )


def test_fake_client_still_flags_impl_that_mocks_openai_import():
    from conduit.anticheat.rules import fake_client_findings

    content = (
        "from unittest.mock import MagicMock, patch\n"
        "import openai\n"
        "with patch('openai.chat.completions.create', MagicMock()):\n"
        "    pass\n"
    )
    hits = fake_client_findings(content, "client.py", _packet())
    assert hits and "mocks the official openai SDK" in hits[0]


def test_baseline_soft_fails_preexisting_legacy_kwargs(tmp_path: Path):
    from conduit.anticheat.baseline import save_anticheat_baseline

    rel = "azure_recommendation.py"
    previous = (
        "import openai\n"
        "openai.api_key = 'x'\n"
        "def generate():\n"
        "    return openai.chat.completions.create(\n"
        "        model='gpt-3.5-turbo', messages=[], max_tokens=64\n"
        "    )\n"
    )
    current = previous.replace("gpt-3.5-turbo", "gpt-5.6-terra")
    (tmp_path / rel).write_text(current, encoding="utf-8")
    save_anticheat_baseline(tmp_path, [rel])

    pkt = _packet()
    pkt["rules"] = list(pkt["rules"]) + [
        {
            "type": "AST_PARAM_RENAME",
            "function_target": "chat.completions.create",
            "old_param": "max_tokens",
            "new_param": "max_completion_tokens",
            "new_callee": "chat.completions.create",
        },
    ]
    report = run_anticheat_mechanical(
        tmp_path,
        pkt,
        [rel],
        previous={rel: previous},
    )
    assert not report.failed, report.findings


def test_baseline_still_flags_new_legacy_kwargs(tmp_path: Path):
    rel = "client.py"
    previous = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "client.chat.completions.create(model='m', messages=[])\n"
    )
    current = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "client.chat.completions.create(model='m', messages=[], max_tokens=64)\n"
    )
    (tmp_path / rel).write_text(current, encoding="utf-8")
    pkt = _packet()
    pkt["rules"] = list(pkt["rules"]) + [
        {
            "type": "AST_PARAM_RENAME",
            "function_target": "chat.completions.create",
            "old_param": "max_tokens",
            "new_param": "max_completion_tokens",
            "new_callee": "chat.completions.create",
        },
    ]
    report = run_anticheat_mechanical(
        tmp_path,
        pkt,
        [rel],
        previous={rel: previous},
    )
    assert report.failed
    assert any("max_tokens" in f for f in report.findings)


def test_baseline_soft_fails_engine_across_completion_callee_rename(tmp_path: Path):
    """Pre-apply Completion.create(engine=) → post completions.create(engine=) is debt."""
    rel = "azure_recommendation.py"
    previous = (
        "import openai\n"
        "openai.api_key = 'x'\n"
        "def generate():\n"
        "    return openai.Completion.create(\n"
        "        engine='text-davinci-003', prompt='hi', max_tokens=64\n"
        "    )\n"
    )
    current = (
        "import openai\n"
        "openai.api_key = 'x'\n"
        "def generate():\n"
        "    return openai.completions.create(\n"
        "        engine='gpt-5.6-terra', prompt='hi', max_tokens=64\n"
        "    )\n"
    )
    (tmp_path / rel).write_text(current, encoding="utf-8")
    pkt = _packet()
    pkt["anticheat"] = {
        "banned_kwargs_on": {
            "completions.create": ["engine"],
            "openai.completions.create": ["engine"],
        }
    }
    report = run_anticheat_mechanical(
        tmp_path,
        pkt,
        [rel],
        previous={rel: previous},
    )
    assert not report.failed, report.findings


def test_baseline_still_flags_new_engine_kwarg(tmp_path: Path):
    rel = "client.py"
    previous = (
        "import openai\n"
        "def generate():\n"
        "    return openai.completions.create(model='m', prompt='hi')\n"
    )
    current = (
        "import openai\n"
        "def generate():\n"
        "    return openai.completions.create(engine='m', prompt='hi')\n"
    )
    (tmp_path / rel).write_text(current, encoding="utf-8")
    pkt = _packet()
    pkt["anticheat"] = {
        "banned_kwargs_on": {
            "completions.create": ["engine"],
            "openai.completions.create": ["engine"],
        }
    }
    report = run_anticheat_mechanical(
        tmp_path,
        pkt,
        [rel],
        previous={rel: previous},
    )
    assert report.failed
    assert any("engine=" in f for f in report.findings)
