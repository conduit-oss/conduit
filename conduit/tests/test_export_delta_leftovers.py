"""Export-delta discovery, path bridge, leftovers, and OpenAI client chain."""

from __future__ import annotations

from pathlib import Path

from conduit.detect.coverage import build_coverage_report
from conduit.detect.client_state import PackageClientState
from conduit.export_delta import ExportDelta
from conduit.export_delta.extract import _python_class_method_exports
from conduit.export_delta.path_bridge import rules_from_path_bridge
from conduit.export_delta.resources import extract_resource_paths, resource_path_for
from conduit.export_delta.usage import (
    PackageCall,
    call_hits_removed,
    collect_package_calls,
    leftover_calls,
)
from conduit.packet.scope import filter_rules_to_source
from conduit.patcher.leftovers import leftovers_failure, leftover_handoff_paths, scan_leftovers
from conduit.patcher.openai_client_chain import transform_openai_client_chain


_AUDIO_SRC = '''
from openai.api_resources.abstract import APIResource

class Audio(APIResource):
    OBJECT_NAME = "audio"

    @classmethod
    def transcribe(cls, model, file, **params):
        url = cls._get_url("transcriptions")
        return url

    @classmethod
    def translate(cls, model, file, **params):
        url = cls._get_url("translations")
        return url
'''

_CHAT_SRC = '''
class ChatCompletion:
    OBJECT_NAME = "chat.completions"

    @classmethod
    def create(cls, *args, **kwargs):
        return None
'''


def test_class_method_exports_include_audio_transcribe():
    symbols = _python_class_method_exports(_AUDIO_SRC)
    assert "Audio.transcribe" in symbols
    assert "Audio.translate" in symbols


def test_resource_paths_from_object_name_and_url_action(tmp_path: Path):
    pkg = tmp_path / "openai"
    resources = pkg / "api_resources"
    resources.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .api_resources.audio import Audio\n", encoding="utf-8")
    (resources / "__init__.py").write_text("", encoding="utf-8")
    (resources / "audio.py").write_text(_AUDIO_SRC, encoding="utf-8")
    (resources / "chat_completion.py").write_text(_CHAT_SRC, encoding="utf-8")
    table = extract_resource_paths(tmp_path, package="openai")
    assert resource_path_for("Audio.transcribe", table) == "/v1/audio/transcriptions"
    assert resource_path_for("openai.Audio.translate", table) == "/v1/audio/translations"
    assert resource_path_for("ChatCompletion.create", table) == "/v1/chat/completions"


def test_consumer_call_hits_removed_without_regex(tmp_path: Path):
    src = tmp_path / "podcast_ingest.py"
    src.write_text(
        "import openai\n\ndef run(f):\n    return openai.Audio.transcribe(model='whisper-1', file=f)\n",
        encoding="utf-8",
    )
    calls = collect_package_calls(tmp_path, [src], "openai")
    assert any(c.callee.endswith("Audio.transcribe") for c in calls)
    assert call_hits_removed("openai.Audio.transcribe", {"Audio", "Audio.transcribe"})
    hits = leftover_calls(calls, {"Audio"})
    assert hits


def test_path_bridge_emits_audio_rewrite_not_unknown():
    calls = [
        PackageCall(rel="podcast_ingest.py", callee="openai.Audio.transcribe", lineno=10),
        PackageCall(rel="x.py", callee="openai.NewThing.foo", lineno=1),
    ]
    paths = {
        "Audio.transcribe": "/v1/audio/transcriptions",
        "openai.Audio.transcribe": "/v1/audio/transcriptions",
    }
    rules = rules_from_path_bridge(
        resource_paths=paths,
        calls=calls,
        removed={"Audio", "Audio.transcribe", "NewThing", "NewThing.foo"},
    )
    olds = {r["old_callee"] for r in rules}
    news = {r["new_callee"] for r in rules}
    assert "Audio.transcribe" in olds
    assert "audio.transcriptions.create" in news
    assert "NewThing.foo" not in olds


def test_client_chain_does_not_half_edit_legacy_whisper():
    src = '''
import openai

# using the legacy OpenAI 0.28 chat API
def transcribe(f):
    resp = openai.Audio.transcribe(model="whisper-1", file=f)
    return (resp.get("text") or "").strip()
'''
    out, details = transform_openai_client_chain(src, to_major=3)
    assert out == src
    assert details == []
    assert "resp.text" not in out
    assert "openai.Audio.transcribe" in out


def test_false_rename_still_leftover_and_path_bridge():
    calls = [
        PackageCall(rel="podcast_ingest.py", callee="openai.Audio.transcribe", lineno=20),
    ]
    gone = {"Audio"}
    assert leftover_calls(calls, gone)
    assert call_hits_removed("openai.Audio.transcribe", gone)
    paths = {
        "Audio.transcribe": "/v1/audio/transcriptions",
        "openai.Audio.transcribe": "/v1/audio/transcriptions",
    }
    rules = rules_from_path_bridge(
        resource_paths=paths,
        calls=calls,
        removed=gone,
    )
    news = {r["new_callee"] for r in rules}
    assert "audio.transcriptions.create" in news
    delta = ExportDelta(
        package="openai",
        from_version="0.28.1",
        to_version="3.3.1",
        ecosystem="pypi",
        removed=set(),
        renamed={"Audio": "audio"},
    )
    assert "Audio" in delta.gone_symbols
    items = scan_leftovers(
        root=Path("."),
        calls=calls,
        delta=delta,
        packet={"to_version": "3.3.1"},
    )
    assert any(i.callee.endswith("Audio.transcribe") for i in items)


def test_collect_calls_strips_utf8_bom(tmp_path: Path):
    src = tmp_path / "podcast_ingest.py"
    src.write_bytes(
        b"\xef\xbb\xbfimport openai\n\ndef run(f):\n"
        b"    return openai.Audio.transcribe(model='whisper-1', file=f)\n"
    )
    calls = collect_package_calls(tmp_path, [src], "openai")
    assert any(c.callee.endswith("Audio.transcribe") for c in calls)


def test_path_bridge_when_to_pin_still_exports_audio():
    calls = [
        PackageCall(rel="podcast_ingest.py", callee="openai.Audio.transcribe", lineno=20),
    ]
    paths = {
        "Audio.transcribe": "/v1/audio/transcriptions",
        "openai.Audio.transcribe": "/v1/audio/transcriptions",
    }
    assert not leftover_calls(calls, set())
    rules = rules_from_path_bridge(resource_paths=paths, calls=calls, removed=set())
    news = {r["new_callee"] for r in rules}
    assert "audio.transcriptions.create" in news
    delta = ExportDelta(
        package="openai",
        from_version="0.28.1",
        to_version="3.3.1",
        ecosystem="pypi",
        removed=set(),
        renamed={},
        resource_paths=paths,
    )
    items = scan_leftovers(
        root=Path("."),
        calls=calls,
        delta=delta,
        packet={"to_version": "3.3.1"},
    )
    assert any(
        i.callee.endswith("Audio.transcribe") and "resource" in i.reason
        for i in items
    )


def test_scope_keeps_path_bridge_rule_when_source_is_chat_only():
    rules = [
        {
            "type": "AST_CALL_REWRITE",
            "old_callee": "Audio.transcribe",
            "new_callee": "audio.transcriptions.create",
            "reason": "Export-delta path bridge: Audio.transcribe → /v1/audio/transcriptions → audio.transcriptions.create",
        },
        {
            "type": "AST_CALL_REWRITE",
            "old_callee": "FineTune.create",
            "new_callee": "fine_tuning.jobs.create",
            "reason": "catalog",
        },
    ]
    source = {
        "api_patterns": ["openai.ChatCompletion.create"],
        "usages": [
            {
                "id": "openai.ChatCompletion.create",
                "callees": ["openai.ChatCompletion.create"],
            }
        ],
        "model_ids": ["gpt-4o"],
    }
    result = filter_rules_to_source(rules, source)
    olds = {r["old_callee"] for r in result.rules}
    assert "Audio.transcribe" in olds
    assert "FineTune.create" not in olds


def test_client_chain_rewrites_whisper_and_strips_guard():
    src = '''
import os
import openai

openai.api_key = os.getenv("OPENAI_API_KEY")

def _openai_has_legacy_audio() -> bool:
    return int(str(getattr(openai, "__version__", "0")).split(".")[0]) < 1

def transcribe(f):
    if not _openai_has_legacy_audio():
        print("Whisper needs openai<1.0; falling through.")
        return None
    resp = openai.audio.transcriptions.create(model="whisper-1", file=f)
    return (resp.get("text") or "").strip()
'''
    out, details = transform_openai_client_chain(src, to_major=3)
    assert "client.audio.transcriptions.create" in out
    assert "OpenAI(api_key=" in out
    assert "from openai import OpenAI" in out
    assert "resp.get(" not in out
    assert "resp.text" in out
    assert "_openai_has_legacy_audio" not in out
    assert "Audio.transcribe" not in out
    assert details


def test_leftovers_fail_unfixed_audio():
    calls = [PackageCall(rel="podcast_ingest.py", callee="openai.Audio.transcribe", lineno=20)]
    delta = ExportDelta(
        package="openai",
        from_version="0.28.1",
        to_version="3.3.1",
        ecosystem="pypi",
        removed={"Audio", "Audio.transcribe"},
    )
    items = scan_leftovers(root=Path("."), calls=calls, delta=delta, packet={"to_version": "3.3.1"})
    assert items
    result = leftovers_failure(items)
    assert not result.passed
    assert "incomplete_migration" in result.fail_reason
    assert leftover_handoff_paths(items) == ["podcast_ingest.py"]


def test_coverage_chatcompletion_will_migrate_on_apply_packet():
    state = PackageClientState(
        package="openai",
        api_patterns=["openai.ChatCompletion.create", "Audio.transcribe"],
    )
    packet = {
        "package": "openai",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "ChatCompletion.create",
                "new_callee": "chat.completions.create",
            },
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "Audio.transcribe",
                "new_callee": "audio.transcriptions.create",
            },
        ],
    }
    report = build_coverage_report(
        package="openai", state=state, signals=[], packet=packet
    )
    by_val = {i.value: i.status for i in report.items}
    assert by_val.get("openai.ChatCompletion.create") == "will_migrate"
    assert by_val.get("Audio.transcribe") == "will_migrate"
