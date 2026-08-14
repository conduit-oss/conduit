"""Source packet + migration coverage diff."""

from __future__ import annotations

from conduit.detect.client_state import PackageClientState
from conduit.detect.coverage import (
    build_coverage_report,
    format_coverage_report,
    save_source_packet,
)
from conduit.detect.models import ChangeSignal


def test_coverage_marks_missed_and_caught_models(tmp_path):
    state = PackageClientState(
        package="openai",
        installed_version="1.40.0",
        model_ids=["gpt-4-0613", "gpt-4o", "mystery-model"],
        api_patterns=["chat.completions"],
        import_files=["app.py"],
        source="regex",
    )
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="MODEL_DEPRECATION",
            affected_pattern="gpt-4-0613",
            replacement_pattern="gpt-4o",
            description="deprecated",
            suggested_rules=[
                {
                    "type": "EXACT_STRING_REPLACE",
                    "target_files": ["*.py"],
                    "match": "gpt-4-0613",
                    "replace": "gpt-4o",
                }
            ],
        )
    ]
    packet = {
        "packet_id": "p",
        "package": "openai",
        "from_version": "1.0",
        "to_version": "2.0",
        "rules": signals[0].suggested_rules,
        "sources": [],
        "notes": "",
    }
    report = build_coverage_report(
        package="openai", state=state, signals=signals, packet=packet
    )
    by_val = {i.value: i for i in report.items}
    assert by_val["gpt-4-0613"].status == "caught"
    assert by_val["mystery-model"].status == "missed"
    assert by_val["chat.completions"].status in {"caught", "missed", "unknown"}

    text = format_coverage_report(report)
    assert "source packet" in text.lower()
    assert "migration packet" in text.lower()
    assert "MISSED" in text
    assert "CAUGHT" in text

    path = save_source_packet(tmp_path, report.source_packet)
    assert path.is_file()
    assert "gpt-4-0613" in path.read_text(encoding="utf-8")


def test_coverage_marks_callee_from_usage_and_rule():
    state = PackageClientState(
        package="openai",
        model_ids=["text-davinci-edit-001"],
        api_patterns=["Edit.create"],
        usages=[
            {
                "id": "text-davinci-edit-001",
                "callees": ["Edit.create"],
                "paths": ["/v1/edits"],
                "files": ["edits.py"],
            }
        ],
    )
    packet = {
        "packet_id": "p",
        "package": "openai",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "Edit.create",
                "new_callee": "chat.completions.create",
            },
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "text-davinci-edit-001",
                "replace": "gpt-4o",
            },
        ],
    }
    report = build_coverage_report(
        package="openai", state=state, signals=[], packet=packet
    )
    by_val = {i.value: i for i in report.items}
    assert by_val["text-davinci-edit-001"].status == "caught"
    assert by_val["Edit.create"].status == "caught"
    assert not report.missed


def test_coverage_skips_usage_id_false_models(monkeypatch):
    """Helper usage ids in model_ids must not count as missed models."""
    monkeypatch.setattr(
        "conduit.detect.modules.openai.known_models.collect_known_model_ids",
        lambda **kwargs: {"gpt-4-0613"},
    )
    state = PackageClientState(
        package="openai",
        model_ids=["apply_edit", "gpt-4-0613"],
        api_patterns=["openai.Edit.create"],
        usages=[
            {
                "id": "apply_edit",
                "callees": ["openai.Edit.create"],
                "paths": [],
                "files": ["edits.py"],
            }
        ],
    )
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="MODEL_DEPRECATION",
            affected_pattern="gpt-4-0613",
            replacement_pattern="gpt-4o",
            description="deprecated",
            suggested_rules=[
                {
                    "type": "EXACT_STRING_REPLACE",
                    "target_files": ["*.py"],
                    "match": "gpt-4-0613",
                    "replace": "gpt-4o",
                }
            ],
        )
    ]
    packet = {
        "packet_id": "p",
        "package": "openai",
        "rules": signals[0].suggested_rules,
        "sources": [],
        "notes": "",
    }
    report = build_coverage_report(
        package="openai", state=state, signals=signals, packet=packet
    )
    model_items = {i.value: i for i in report.items if i.kind == "model"}
    assert "apply_edit" not in model_items
    assert model_items["gpt-4-0613"].status == "caught"
    assert "apply_edit" not in {i.value for i in report.missed if i.kind == "model"}


def test_empty_source_notes_unknown_baseline():
    report = build_coverage_report(
        package="openai",
        state=PackageClientState(package="openai"),
        signals=[],
        packet={"packet_id": "x", "rules": []},
    )
    assert any("empty" in n.lower() or "unknown" in n.lower() for n in report.notes)
