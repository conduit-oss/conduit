"""Mechanical post-rule inference from failures and repo scans."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from conduit.test_runner import TestResult

_LIST_FAIL_RE = re.compile(r"isinstance\(.*,\s*list\)|not a list|return list", re.I)
_FLAGGED_FAIL_RE = re.compile(r"flagged|moderation", re.I)
_MAX_TOKENS_RE = re.compile(
    r"Unsupported parameter: 'max_tokens'.*max_completion_tokens",
    re.I | re.S,
)


def _glob_target(rel: str) -> list[str]:
    return [rel.replace("\\", "/")]


def infer_from_test_result(
    result: TestResult,
    *,
    file_windows: list[dict[str, Any]] | None = None,
    packet: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Infer post-rules from pytest failure text and file windows."""
    blob = f"{result.stdout or ''}\n{result.stderr or ''}"
    rules: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def _add(rule: dict[str, Any]) -> None:
        key = (
            rule.get("type"),
            tuple(rule.get("target_files") or ()),
            rule.get("function_name"),
        )
        if key in seen:
            return
        seen.add(key)
        rule.setdefault("source", "mechanical")
        rules.append(rule)

    for win in file_windows or []:
        if not isinstance(win, dict):
            continue
        rel = str(win.get("path") or "").replace("\\", "/")
        text = str(win.get("text") or "")
        if not rel or not text:
            continue

        if "files.list" in text or "File.list" in text:
            if "return list(data)" not in text and "def list_files" in text:
                if _LIST_FAIL_RE.search(blob) or '["data"]' in text or "['data']" in text:
                    _add(
                        {
                            "type": "WRAPPER_ENSURE_LIST",
                            "target_files": _glob_target(rel),
                            "function_name": "list_files",
                            "list_callee_hint": "openai.files.list()",
                            "reason": "List API should return plain list, not page/dict.",
                        }
                    )

        if "fine_tuning.jobs.list" in text or "FineTune.list" in text:
            if "return list(data)" not in text and "def list_fine_tunes" in text:
                _add(
                    {
                        "type": "WRAPPER_ENSURE_LIST",
                        "target_files": _glob_target(rel),
                        "function_name": "list_fine_tunes",
                        "list_callee_hint": "openai.fine_tuning.jobs.list()",
                        "reason": "Fine-tune list helper should return plain list.",
                    }
                )

        if "moderations.create" in text or "Moderation.create" in text:
            if "def moderate_text" in text and (
                _FLAGGED_FAIL_RE.search(blob)
                or '"flagged"' not in text
            ):
                _add(
                    {
                        "type": "WRAPPER_ENSURE_DICT_KEY",
                        "target_files": _glob_target(rel),
                        "function_name": "moderate_text",
                        "required_key": "flagged",
                        "create_callee_hint": "openai.moderations.create(**kwargs)",
                        "reason": "Moderation helper must return dict with flagged.",
                    }
                )

        if "def complete_with_engine" in text and "chat.completions.create" in text:
            if "return complete_prompt(" not in text:
                _add(
                    {
                        "type": "WRAPPER_DELEGATE",
                        "target_files": _glob_target(rel),
                        "function_name": "complete_with_engine",
                        "delegate_module": "openai_text.completions",
                        "delegate_symbol": "complete_prompt",
                        "model_param": "engine",
                        "reason": "Engine helper should delegate to shared complete_prompt.",
                    }
                )

        if "def complete_prompt" in text and (
            "except Exception:" in text
            or 'model="text-davinci-003"' in text
            or (packet and packet.get("runtime_model_aliases"))
        ):
            if "return OpenAI(api_key=get_api_key())" not in text or "except Exception:" in text:
                _add(
                    {
                        "type": "RUNTIME_MODEL_ALIAS",
                        "target_files": _glob_target(rel),
                        "function_name": "complete_prompt",
                        "replace_module": True,
                        "reason": "Completion helper needs runtime model aliasing.",
                    }
                )

    if _MAX_TOKENS_RE.search(blob):
        for rule in (packet or {}).get("rules") or []:
            if not isinstance(rule, dict):
                continue
            if rule.get("type") == "AST_PARAM_RENAME":
                continue
        # Param rename handled by failure_rules; post stage uses same AST apply via packet merge

    return rules


def infer_from_repo_scan(
    root: Path,
    paths: list[str],
    *,
    packet: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan candidate wrapper files for structural gaps (pre-test)."""
    root = root.resolve()
    windows: list[dict[str, Any]] = []
    for rel in paths:
        path = root / rel.replace("/", "\\")
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        windows.append({"path": rel.replace("\\", "/"), "text": text})
    fake = TestResult(
        runner="scan",
        passed=False,
        returncode=1,
        stdout="",
        stderr="",
        command=[],
    )
    return infer_from_test_result(fake, file_windows=windows, packet=packet)
