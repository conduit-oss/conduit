"""LLM self-correction loop after packet apply (tests + production files)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from conduit.anticheat.audit_log import MigrationAuditLog
from conduit.anticheat.rules import reject_write
from conduit.anticheat.scan import anticheat_failure_result, run_anticheat
from conduit.llm import attach_llm_log, get_llm_client
from conduit.repair_ignore import IgnoreList, build_ignore_list
from conduit.test_runner import TestResult, run_tests

LogFn = Callable[[str], None]

_PATH_RE = re.compile(r"/v1/[a-z0-9/_-]+", re.I)
_QUOTED_ID_RE = re.compile(r"""[`'"]([A-Za-z0-9._/-]{3,})[`'"]""")
_PYTEST_COUNT_RE = re.compile(
    r"(?P<failed>\d+)\s+failed|(?P<passed>\d+)\s+passed|(?P<error>\d+)\s+error",
    re.I,
)
_FAILED_NODE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)
_STILL_CONTAINS_RE = re.compile(r"still contains\s+'([^']+)'", re.I)
# Oracle-style: "configs/foo.json still contains 'davinci'"
_LEFTOVER_PATH_RE = re.compile(
    r"(?m)^(?P<path>[^\s:]+?)\s+(?:still contains|obfuscates leftover)\s+'(?P<token>[^']+)'",
    re.I,
)
# Pytest short-tb / bare exception lines (generic — not API-specific).
_PYTEST_E_LINE_RE = re.compile(r"(?m)^E\s+(\S.+)$")
_EXCEPTION_LINE_RE = re.compile(
    r"(?m)^([A-Za-z_][\w.]*(?:Error|Exception|Warning):\s*.+)$"
)
_CONTEXT_IMPL_CAP = 8
_LEFTOVER_PATH_CAP = 16
_WINDOW_RADIUS = 60
_PROMPT_STREAM_HEAD = 1000
_PROMPT_STREAM_TAIL = 2000
_PROMPT_STREAM_CHARS = _PROMPT_STREAM_TAIL  # back-compat alias
_EXCEPTION_SNIPPET_CAP = 8


def _noop_log(_: str) -> None:
    return None


def _failure_blob(result: TestResult) -> str:
    return f"{result.stdout or ''}\n{result.stderr or ''}"


def _is_anticheat_failure(result: TestResult) -> bool:
    return str(result.runner or "").lower() == "anticheat"


def _failure_fingerprint(result: TestResult) -> str:
    """Stable signature of what is still failing (for stagnant early-stop)."""
    if _is_anticheat_failure(result):
        body = (result.stdout or "").strip()
        return f"anticheat:{body}"
    blob = _failure_blob(result)
    nodes = sorted(set(_FAILED_NODE_RE.findall(blob)))
    leftovers = sorted(set(_STILL_CONTAINS_RE.findall(blob)))
    counts = (
        result.failed_count,
        result.error_count,
        result.passed_count,
    )
    if nodes or leftovers:
        return f"nodes={nodes}|left={leftovers}|counts={counts}"
    snippets = _exception_snippets(blob, limit=4)
    if snippets:
        return f"snippets={snippets}|counts={counts}"
    reason = (result.fail_reason or "").strip()
    excerpt = re.sub(r"\s+", " ", blob).strip()[:400]
    return f"reason={reason}|excerpt={excerpt}|counts={counts}"


def _failed_nodes(result: TestResult) -> list[str]:
    return sorted(set(_FAILED_NODE_RE.findall(_failure_blob(result))))


def _leftover_tokens(result: TestResult) -> list[str]:
    return sorted(set(_STILL_CONTAINS_RE.findall(_failure_blob(result))))


def _leftover_path_hits(text: str) -> list[tuple[str, str]]:
    """(rel_path, token) pairs from leftover-oracle assertion lines."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in _LEFTOVER_PATH_RE.finditer(text or ""):
        rel = m.group("path").replace("\\", "/").strip().lstrip("./")
        token = m.group("token")
        if not rel or not token:
            continue
        key = (rel, token)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _pack_stream(
    text: str,
    *,
    head: int = _PROMPT_STREAM_HEAD,
    tail: int = _PROMPT_STREAM_TAIL,
) -> str:
    """Keep start and end of a long stream so early FAILED lines are not dropped."""
    text = text or ""
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n...\n{text[-tail:]}"


def _exception_snippets(text: str, *, limit: int = _EXCEPTION_SNIPPET_CAP) -> list[str]:
    """Unique exception / pytest-E lines from a failure blob (generic)."""
    out: list[str] = []
    seen: set[str] = set()
    for pattern in (_PYTEST_E_LINE_RE, _EXCEPTION_LINE_RE):
        for m in pattern.finditer(text or ""):
            line = re.sub(r"\s+", " ", m.group(1)).strip()
            if len(line) > 240:
                line = line[:237] + "..."
            if not line or line in seen:
                continue
            # Skip leftover-oracle noise already covered by leftover_* fields.
            if "still contains" in line.lower() or "obfuscates leftover" in line.lower():
                continue
            seen.add(line)
            out.append(line)
            if len(out) >= limit:
                return out
    return out


def build_failure_digest(result: TestResult) -> dict[str, Any]:
    """Structured failure digest from full stdout/stderr (for prompt + tools)."""
    blob = _failure_blob(result)
    nodes = _failed_nodes(result)
    leftovers = _leftover_tokens(result)
    leftover_paths = [
        {"path": path, "token": token} for path, token in _leftover_path_hits(blob)
    ]
    snippets = _exception_snippets(blob)
    lines: list[str] = []
    if nodes:
        lines.append("failed_nodes: " + ", ".join(nodes[:12]))
        if len(nodes) > 12:
            lines[-1] += f" (+{len(nodes) - 12} more)"
    if leftover_paths:
        preview = "; ".join(
            f"{p['path']} still contains '{p['token']}'" for p in leftover_paths[:8]
        )
        lines.append("leftovers: " + preview)
        if len(leftover_paths) > 8:
            lines[-1] += f" (+{len(leftover_paths) - 8} more)"
    elif leftovers:
        lines.append("leftover_tokens: " + ", ".join(leftovers[:12]))
    for snip in snippets[:6]:
        lines.append(snip)
    return {
        "failed_nodes": nodes,
        "leftover_tokens": leftovers[:80],
        "leftover_files": leftover_paths[:_LEFTOVER_PATH_CAP],
        "exception_snippets": snippets,
        "lines": lines,
        "text": "\n".join(lines),
    }


def _structured_failure(result: TestResult) -> dict[str, Any]:
    """Compact failure signal for the repair prompt (Cursor-shaped)."""
    digest = build_failure_digest(result)
    return {
        "failed_nodes": digest["failed_nodes"],
        "leftover_tokens": digest["leftover_tokens"],
        "leftover_files": digest["leftover_files"],
        "exception_snippets": digest["exception_snippets"],
        "failure_digest": digest["text"],
        "failure_fingerprint": _failure_fingerprint(result),
        "fail_reason": (result.fail_reason or "").strip(),
        "runner": result.runner,
        "failed_count": result.failed_count,
        "error_count": result.error_count,
        "passed_count": result.passed_count,
    }


def _mark_unpassable(result: TestResult, reason: str) -> TestResult:
    """Attach an early-stop reason visible in TestResult.summary."""
    reason = reason.strip()
    if not reason:
        return result
    if result.fail_reason:
        if reason not in result.fail_reason:
            result.fail_reason = f"{reason}; {result.fail_reason}"
    else:
        result.fail_reason = reason
    return result


def _emit_unpassable_stop(emit: LogFn, reason: str) -> None:
    emit(f"[self-correct] stopping early: {reason}")


@dataclass
class FixAttempt:
    strategy: str  # llm | heuristic | none
    files: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


@dataclass
class LlmRepairSuggestion:
    files: dict[str, str] = field(default_factory=dict)
    search_queries: list[str] = field(default_factory=list)
    packet_patch: dict[str, Any] = field(default_factory=dict)
    snapshots: dict[str, str | None] = field(default_factory=dict)


def _failure_excerpt(test_result: TestResult, *, limit: int = 1500) -> str:
    parts = []
    if test_result.stdout and test_result.stdout.strip():
        parts.append(test_result.stdout.strip())
    if test_result.stderr and test_result.stderr.strip():
        parts.append(test_result.stderr.strip())
    text = "\n".join(parts).strip() or "(no stdout/stderr captured)"
    if len(text) > limit:
        return "…\n" + text[-limit:]
    return text


# pytest short traceback: "openai_text\engines.py:25: in complete_with_engine"
_PYTEST_PATH_RE = re.compile(
    r"(?m)^(?P<path>(?:[A-Za-z]:)?[^:\n]+\.(?:py|pyw|ts|js|tsx|jsx|go|java))"
    r":(?P<line>\d+)(?::|\s)"
)
_FILE_LINE_RE = re.compile(
    r'File "(?P<path>[^"]+)", line (?P<line>\d+)'
)


def _candidate_path(root: Path, raw: str) -> Path | None:
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return None
    raw = raw.replace("\\", "/")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        if resolved.is_file() and (
            resolved == root_resolved or root_resolved in resolved.parents
        ):
            return resolved
    except OSError:
        return None
    return None


def _traceback_hits(
    root: Path, text: str, *, limit: int = 12
) -> list[tuple[Path, int | None]]:
    """(path, 1-based line or None) from pytest / Python tracebacks."""
    found: list[tuple[Path, int | None]] = []
    seen: set[str] = set()

    def _add(path: Path, line: int | None) -> None:
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        found.append((path, line))

    for m in _FILE_LINE_RE.finditer(text or ""):
        path = _candidate_path(root, m.group("path"))
        if path is not None:
            try:
                line = int(m.group("line"))
            except ValueError:
                line = None
            _add(path, line)
        if len(found) >= limit:
            return found
    for m in _PYTEST_PATH_RE.finditer(text or ""):
        path = _candidate_path(root, m.group("path"))
        if path is not None:
            try:
                line = int(m.group("line"))
            except ValueError:
                line = None
            _add(path, line)
        if len(found) >= limit:
            break
    return found


def _paths_from_traceback(root: Path, text: str, limit: int = 12) -> list[Path]:
    return [p for p, _ in _traceback_hits(root, text, limit=limit)]


def _file_window(
    path: Path, line: int | None, *, radius: int = _WINDOW_RADIUS
) -> dict[str, Any]:
    """Read ±radius lines around line (1-based); whole file if short / no line."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines:
        return {"start": 1, "end": 1, "text": "", "line": line}
    if line is None or line < 1:
        if len(lines) <= radius * 2 + 1:
            return {
                "start": 1,
                "end": len(lines),
                "text": text,
                "line": line,
            }
        # Head of file when no line number
        end = min(len(lines), radius * 2 + 1)
        return {
            "start": 1,
            "end": end,
            "text": "".join(lines[:end]),
            "line": line,
        }
    idx = min(max(line, 1), len(lines)) - 1
    start = max(0, idx - radius)
    end = min(len(lines), idx + radius + 1)
    return {
        "start": start + 1,
        "end": end,
        "text": "".join(lines[start:end]),
        "line": line,
    }


def _package_dirs(root: Path) -> list[Path]:
    """Top-level Python package dirs (exclude tests/src special-casing)."""
    skip = {
        "tests",
        "test",
        "src",
        "docs",
        "examples",
        "scripts",
        "venv",
        ".venv",
        "node_modules",
        "build",
        "dist",
        ".git",
        ".conduit",
        "__pycache__",
    }
    out: list[Path] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return out
    for child in children:
        if not child.is_dir() or child.name in skip or child.name.startswith("."):
            continue
        if (child / "__init__.py").is_file():
            out.append(child)
    return out


def _is_test_rel(rel: str) -> bool:
    posix = rel.replace("\\", "/").lower()
    name = Path(posix).name
    return (
        posix.startswith("tests/")
        or posix.startswith("test/")
        or name.startswith("test_")
        or name == "conftest.py"
    )


def _neighbor_sources(path: Path, *, limit: int = 6) -> list[Path]:
    parent = path.parent
    if not parent.is_dir():
        return []
    out: list[Path] = []
    try:
        siblings = sorted(parent.iterdir())
    except OSError:
        return []
    for sib in siblings:
        if not sib.is_file():
            continue
        if sib.suffix.lower() not in {".py", ".ts", ".js", ".tsx", ".jsx"}:
            continue
        if sib == path:
            continue
        out.append(sib)
        if len(out) >= limit:
            break
    return out


@dataclass
class RepairContext:
    """Cursor-shaped repair seed: windows + allowlist, not full-module dumps."""

    file_windows: list[dict[str, Any]] = field(default_factory=list)
    seeded_paths: list[str] = field(default_factory=list)
    allowlist: set[str] = field(default_factory=set)
    # Path → window text for prompt "files" (compat with older callers)
    files: dict[str, str] = field(default_factory=dict)
    # Leftover-oracle offenders: [{path, tokens}]
    leftover_files: list[dict[str, Any]] = field(default_factory=list)


def collect_repair_context(
    root: Path,
    test_result: TestResult,
    *,
    packet: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    impl_cap: int = _CONTEXT_IMPL_CAP,
) -> RepairContext:
    """Build span windows + path allowlist for self-correct."""
    root = root.resolve()
    blob = _failure_blob(test_result)
    hits = _traceback_hits(root, blob)
    ctx = RepairContext()

    def _rel(path: Path) -> str:
        try:
            return str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            return path.name.replace("\\", "/")

    def _register(path: Path, line: int | None, *, as_window: bool) -> None:
        rel = _rel(path)
        ctx.allowlist.add(rel)
        if rel not in ctx.seeded_paths:
            ctx.seeded_paths.append(rel)
        if not as_window or rel in ctx.files:
            return
        try:
            win = _file_window(path, line)
        except OSError:
            return
        ctx.files[rel] = win["text"]
        ctx.file_windows.append(
            {
                "path": rel,
                "start": win["start"],
                "end": win["end"],
                "line": win["line"],
                "text": win["text"],
            }
        )

    impl_hits = [(p, ln) for p, ln in hits if not _is_test_rel(_rel(p))]
    test_hits = [(p, ln) for p, ln in hits if _is_test_rel(_rel(p))]

    for path, line in impl_hits[:impl_cap]:
        _register(path, line, as_window=True)
        init = path.parent / "__init__.py"
        if init.is_file():
            _register(init, None, as_window=False)
        for sib in _neighbor_sources(path):
            ctx.allowlist.add(_rel(sib))

    # Only import_files that appear in the traceback (avoid flood).
    trace_rels = {_rel(p) for p, _ in hits}
    import_files: list[str] = []
    for rel in (source or {}).get("import_files") or []:
        if isinstance(rel, str):
            import_files.append(rel.replace("\\", "/"))
    for rel in (packet or {}).get("import_files") or []:
        if isinstance(rel, str) and rel.replace("\\", "/") not in import_files:
            import_files.append(rel.replace("\\", "/"))
    for rel in import_files:
        if _is_test_rel(rel):
            continue
        if rel not in trace_rels:
            continue
        path = root / rel
        if path.is_file():
            _register(path, None, as_window=len(ctx.file_windows) < impl_cap)

    conftest = root / "tests" / "conftest.py"
    if conftest.is_file():
        _register(conftest, None, as_window=True)
    root_conftest = root / "conftest.py"
    if root_conftest.is_file():
        _register(root_conftest, None, as_window=True)

    # Primary failing test file only (first traceback test hit).
    for path, line in test_hits[:1]:
        _register(path, line, as_window=True)

    # Seed files named by leftover-oracle "path still contains 'token'" lines.
    from conduit.test_gen import is_conduit_generated_rel

    by_path: dict[str, list[str]] = {}
    for rel, token in _leftover_path_hits(blob):
        if is_conduit_generated_rel(rel):
            continue
        path = root / rel
        if not path.is_file():
            continue
        by_path.setdefault(rel, [])
        if token not in by_path[rel]:
            by_path[rel].append(token)

    leftover_added = 0
    for rel, tokens in by_path.items():
        if leftover_added >= _LEFTOVER_PATH_CAP:
            break
        path = root / rel
        ctx.leftover_files.append({"path": rel, "tokens": tokens})
        # Prefer a window around the first leftover token if present.
        line: int | None = None
        try:
            text = path.read_text(encoding="utf-8")
            for tok in tokens:
                idx = text.find(tok)
                if idx >= 0:
                    line = text.count("\n", 0, idx) + 1
                    break
        except OSError:
            pass
        as_window = len(ctx.file_windows) < impl_cap + _LEFTOVER_PATH_CAP
        _register(path, line, as_window=as_window)
        leftover_added += 1

    return ctx


def _collect_context_files(
    root: Path,
    test_result: TestResult,
    limit: int = 24,
    packet: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Backward-compatible: window texts keyed by path (capped)."""
    ctx = collect_repair_context(
        root,
        test_result,
        packet=packet,
        source=source,
        impl_cap=min(_CONTEXT_IMPL_CAP, limit),
    )
    out: dict[str, str] = {}
    for rel, text in ctx.files.items():
        if len(out) >= limit:
            break
        out[rel] = text
    return out


def reject_self_correct_write(
    rel: str,
    content: str,
    *,
    packet: dict[str, Any],
    ignore: IgnoreList | None = None,
    previous: str | None = None,
    root: Path | None = None,
) -> str | None:
    """Return a reason to drop this write, or None if it is allowed."""
    rel_posix = rel.replace("\\", "/")
    ignore = ignore or IgnoreList()
    if ignore.path_ignored(rel_posix):
        return f"ignored path {rel_posix}"
    if previous is None and root is not None:
        path = root / rel_posix
        if path.is_file():
            try:
                previous = path.read_text(encoding="utf-8")
            except OSError:
                previous = None
        else:
            previous = ""
    return reject_write(
        rel_posix, content, packet=packet, previous=previous
    )


def _apply_file_updates(
    root: Path,
    updates: dict[str, str],
    snapshots: dict[str, str | None] | None = None,
    *,
    packet: dict[str, Any] | None = None,
    ignore: IgnoreList | None = None,
    log: LogFn | None = None,
    audit_log: MigrationAuditLog | None = None,
    attempt: int | None = None,
) -> list[str]:
    changed: list[str] = []
    root_resolved = root.resolve()
    store = snapshots if snapshots is not None else {}
    for rel, content in updates.items():
        path = (root / rel).resolve()
        if not str(path).startswith(str(root_resolved)):
            continue
        rel_posix = rel.replace("\\", "/")
        if packet is not None:
            reason = reject_self_correct_write(
                rel_posix,
                content,
                packet=packet,
                ignore=ignore,
                root=root,
            )
            if reason:
                emit = log or _noop_log
                emit(f"[self-correct] rejected obfuscating edit {rel_posix}: {reason}")
                if audit_log is not None:
                    audit_log.record_reject(
                        rel_posix, reason, source="repair", attempt=attempt
                    )
                continue
        previous_text: str | None
        if rel_posix not in store:
            if path.is_file():
                try:
                    store[rel_posix] = path.read_text(encoding="utf-8")
                except OSError:
                    store[rel_posix] = None
            else:
                store[rel_posix] = None
        previous_text = store.get(rel_posix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        changed.append(rel_posix)
        if audit_log is not None:
            audit_log.record_write(
                rel_posix,
                source="repair",
                attempt=attempt,
                before=previous_text,
                after=content,
            )
    return changed


def _append_packet_note(packet: dict[str, Any], note: str) -> None:
    note = note.strip()
    if not note:
        return
    prev = str(packet.get("notes") or "").strip()
    packet["notes"] = f"{prev}\n{note}".strip() if prev else note


def _ids_from_packet(packet: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("type") == "EXACT_STRING_REPLACE":
            for key in ("match", "replace"):
                val = str(rule.get(key) or "").strip()
                if val:
                    ids.append(val)
    return ids


def _quoted_identifiers(text: str, *, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in _QUOTED_ID_RE.finditer(text or ""):
        token = match.group(1).strip()
        if token.lower() in seen or len(token) < 3:
            continue
        seen.add(token.lower())
        out.append(token)
        if len(out) >= limit:
            break
    return out


def _packet_for_prompt(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "packet_id": packet.get("packet_id"),
        "package": packet.get("package"),
        "from_version": packet.get("from_version"),
        "to_version": packet.get("to_version"),
        "rules": list(packet.get("rules") or []),
    }


def _source_for_prompt(source: dict[str, Any] | None) -> dict[str, Any]:
    if not source:
        return {}
    return {
        "installed_version": source.get("installed_version"),
        "model_ids": list(source.get("model_ids") or []),
        "api_patterns": list(source.get("api_patterns") or []),
        "usages": list(source.get("usages") or [])[:20],
        "import_files": list(source.get("import_files") or []),
    }


def _load_source_packet(root: Path, packet: dict[str, Any] | None) -> dict[str, Any]:
    src_dir = root / ".conduit" / "source-packets"
    package = str((packet or {}).get("package") or "").strip()
    candidates: list[Path] = []
    if src_dir.is_dir():
        if package:
            hit = src_dir / f"{package}.json"
            if hit.is_file():
                candidates.append(hit)
        candidates.extend(sorted(src_dir.glob("*.json")))
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _restore_snapshots(root: Path, snapshots: dict[str, str | None]) -> list[str]:
    restored: list[str] = []
    for rel, content in snapshots.items():
        path = root / rel
        if content is None:
            if path.is_file():
                try:
                    path.unlink()
                    restored.append(rel)
                except OSError:
                    continue
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            restored.append(rel)
        except OSError:
            continue
    return restored


def _pytest_counts(result: TestResult) -> tuple[int | None, int | None]:
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    failed = passed = None
    for match in _PYTEST_COUNT_RE.finditer(text):
        if match.group("failed") is not None:
            failed = int(match.group("failed"))
        if match.group("passed") is not None:
            passed = int(match.group("passed"))
    return failed, passed


def _is_collection_error(result: TestResult) -> bool:
    text = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return (
        "error collecting" in text
        or "importerror while loading conftest" in text
        or "cannot import name" in text
        or result.returncode in {2, 4}
    )


def _repair_regressed(previous: TestResult, current: TestResult) -> bool:
    if _is_collection_error(current) and not _is_collection_error(previous):
        return True
    prev_fail, prev_pass = _pytest_counts(previous)
    cur_fail, cur_pass = _pytest_counts(current)
    if prev_pass and cur_pass is not None and cur_pass < max(1, prev_pass // 2):
        return True
    if prev_fail is not None and cur_fail is not None and cur_fail > prev_fail + 4:
        return True
    return False


def _top_level_names(source: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"(?m)^(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", source or ""):
        names.add(match.group(2))
    return names


def _error_search_snippets(test_result: TestResult, *, limit: int = 4) -> list[str]:
    """Turn failure lines into general web-search queries."""
    text = "\n".join(
        [test_result.stdout or "", test_result.stderr or ""]
    )
    queries: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 12:
            continue
        lower = line.lower()
        if not any(
            k in lower
            for k in (
                "error",
                "exception",
                "failed",
                "traceback",
                "assert",
                "typeerror",
                "attributeerror",
                "invalid",
                "deprecated",
                "not found",
                "unsupported",
                "model",
            )
        ):
            continue
        # Drop noisy pytest chrome
        if lower.startswith(("=", "-", "___", "platform ", "rootdir")):
            continue
        q = re.sub(r"\s+", " ", line)[:180]
        if q not in queries:
            queries.append(q)
        if len(queries) >= limit:
            break
    return queries


def _extract_research_targets(
    test_result: TestResult,
    packet: dict[str, Any],
    *,
    extra_queries: list[str] | None = None,
    source: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (seed_urls, search_queries) derived from failure + packet."""
    blob = "\n".join(
        [
            test_result.stdout or "",
            test_result.stderr or "",
            json.dumps(packet.get("rules") or [])[:4000],
        ]
    )
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for mid in [
        *_quoted_identifiers(blob),
        *_ids_from_packet(packet),
        *(str(x) for x in (source or {}).get("model_ids") or []),
    ]:
        key = mid.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered_ids.append(mid)

    paths = sorted({p.lower() for p in _PATH_RE.findall(blob)})
    package = str(packet.get("package") or "")
    profile = None
    try:
        from conduit.detect.vendor_profile import profile_for_package

        if package:
            profile = profile_for_package(package)
    except Exception:
        profile = None

    if profile is not None:
        seeds = list(profile.evidence_seeds)
        if profile.models_catalog_url and profile.models_catalog_url not in seeds:
            seeds.insert(0, profile.models_catalog_url)
        for mid in ordered_ids[:6]:
            url = profile.model_doc_url(mid)
            if url:
                seeds.append(url)
        pkg_label = package or profile.name
    else:
        seeds = []
        pkg_label = package or "package"

    queries: list[str] = [
        f"{pkg_label} API migration test failure",
        f"{pkg_label} python sdk migration breaking change",
    ]
    queries.extend(_error_search_snippets(test_result))
    for mid in ordered_ids[:4]:
        queries.append(f"{pkg_label} {mid} supported endpoints replacement")
    for path in paths[:3]:
        queries.append(f"{pkg_label} {path} replacement deprecation")
    for q in extra_queries or []:
        q = str(q).strip()
        if q and q not in queries:
            queries.append(q)

    return seeds, queries


def _research_for_failure(
    test_result: TestResult,
    packet: dict[str, Any],
    *,
    log: LogFn,
    extra_queries: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Fetch docs via seeds + general web search. Returns (evidence_text, warnings)."""
    from conduit.packet.evidence import build_evidence, evidence_as_prompt_text

    seeds, queries = _extract_research_targets(
        test_result, packet, extra_queries=extra_queries
    )
    allow_hosts = ["github.com"]
    try:
        from conduit.detect.vendor_profile import profile_for_package

        pkg = str(packet.get("package") or "")
        prof = profile_for_package(pkg) if pkg else None
        if prof is not None and prof.evidence_hosts:
            allow_hosts = list(prof.evidence_hosts)
    except Exception:
        pass
    log(
        "[self-correct] researching (general search): "
        f"{len(seeds)} seed URL(s), {len(queries)} quer(ies)"
    )
    docs, warnings = build_evidence(
        seed_urls=seeds,
        allow_hosts=allow_hosts,
        search_queries=queries,
        max_seed_pages=6,
        max_search_hits=5,
        open_search=True,
    )
    for w in warnings:
        log(f"[self-correct] research: {w}")
    if docs:
        log(
            "[self-correct] research fetched: "
            + ", ".join(d.url for d in docs[:8])
            + ("…" if len(docs) > 8 else "")
        )
    text = evidence_as_prompt_text(docs, max_total_chars=28_000)
    return text, warnings


def _apply_packet_patch(packet: dict[str, Any], patch: dict[str, Any]) -> list[str]:
    """Merge LLM packet updates in-place. Returns human-readable change lines."""
    from conduit.packet.validate import validate_packet

    if not patch:
        return []
    details: list[str] = []

    notes = patch.get("notes")
    if isinstance(notes, str) and notes.strip():
        _append_packet_note(packet, notes.strip())
        details.append("updated notes")

    for src in patch.get("sources") or []:
        if not isinstance(src, dict) or not src.get("url"):
            continue
        url = str(src["url"])
        existing = {str(s.get("url")) for s in (packet.get("sources") or []) if isinstance(s, dict)}
        if url in existing:
            continue
        packet.setdefault("sources", []).append(
            {"url": url, "kind": str(src.get("kind") or "other")}
        )
        details.append(f"source {url}")

    new_rules = patch.get("rules")
    if isinstance(new_rules, list) and new_rules:
        rules = list(packet.get("rules") or [])
        by_exact: dict[str, int] = {}
        for i, rule in enumerate(rules):
            if isinstance(rule, dict) and rule.get("type") == "EXACT_STRING_REPLACE":
                by_exact[str(rule.get("match"))] = i
        for rule in new_rules:
            if not isinstance(rule, dict) or not rule.get("type"):
                continue
            rule = dict(rule)
            if rule.get("type") == "EXACT_STRING_REPLACE" and rule.get("match") in by_exact:
                idx = by_exact[str(rule["match"])]
                rules[idx] = rule
                details.append(
                    f"rule replace {rule.get('match')!r} → {rule.get('replace')!r}"
                )
            else:
                rules.append(rule)
                details.append(f"rule add {rule.get('type')}")
        probe = dict(packet)
        probe["rules"] = rules
        errs = validate_packet(probe)
        if errs:
            details.append(f"packet patch rejected (invalid): {errs[:2]}")
        else:
            packet["rules"] = rules

    return details


def _heuristic_fix(
    root: Path, packet: dict[str, Any], ignore: IgnoreList | None = None
) -> FixAttempt:
    from conduit.patcher.string_replace import exact_replace
    from conduit.repair_ignore import exact_replace_respecting_ignore
    from conduit.test_gen import is_conduit_generated_rel

    ignore = ignore or IgnoreList()
    replacements: list[tuple[str, str]] = []
    drop_rules: list[dict[str, Any]] = []
    for rule in packet.get("rules") or []:
        if rule.get("type") == "EXACT_STRING_REPLACE":
            replacements.append((str(rule["match"]), str(rule["replace"])))
        if rule.get("type") == "AST_PARAM_RENAME":
            replacements.append((str(rule["old_param"]), str(rule["new_param"])))
        if rule.get("type") == "AST_PARAM_DROP":
            drop_rules.append(rule)

    # De-dupe while preserving order; longer matches first (gpt-4-0613 before gpt-4)
    seen: set[tuple[str, str]] = set()
    uniq: list[tuple[str, str]] = []
    for pair in replacements:
        if pair in seen or not pair[0] or pair[0] == pair[1]:
            continue
        seen.add(pair)
        uniq.append(pair)
    replacements = sorted(uniq, key=lambda p: len(p[0]), reverse=True)

    targets: list[Path] = []
    # Prefer migration surfaces leftover-oracle already scans — not consumer tests.
    for dirname in ("configs", "scripts", ".github", "src"):
        d = root / dirname
        if d.is_dir():
            targets += [p for p in d.rglob("*") if p.is_file()]
    for pat in (
        "*.yml",
        "*.yaml",
        "*.json",
        "*.sh",
        "*.toml",
        "Dockerfile",
        "docker-compose*.yml",
        "docker-compose*.yaml",
    ):
        targets += [p for p in root.glob(pat) if p.is_file()]
    # Package / service trees (Python + TS) excluding tests/.
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name.lower()
        if name in {
            "tests",
            "test",
            "docs",
            "vendor",
            ".conduit",
            ".git",
            "node_modules",
            "venv",
            ".venv",
            "__pycache__",
            "packets",
        }:
            continue
        if name in {"configs", "scripts", ".github", "src"}:
            continue  # already walked
        targets += [
            p
            for p in child.rglob("*")
            if p.is_file()
            and p.suffix.lower()
            in {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yml", ".yaml", ".sh"}
        ]

    # De-dupe paths while preserving order
    seen_paths: set[Path] = set()
    uniq_targets: list[Path] = []
    for path in targets:
        try:
            key = path.resolve()
        except OSError:
            continue
        if key in seen_paths:
            continue
        seen_paths.add(key)
        uniq_targets.append(path)
    targets = uniq_targets

    changed: list[str] = []
    details: list[str] = []
    match_counts = {old: 0 for old, _ in replacements}
    skipped_files = 0

    from conduit.patcher.ast_param_drop import apply_param_drop

    for path in targets:
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        # Never rewrite leftover/smoke/functional oracles (same as apply / LLM reject).
        # Heuristic also skips tests/ so it does not advance assertions ahead of configs.
        if (
            is_conduit_generated_rel(rel)
            or ignore.path_ignored(rel)
            or _is_test_rel(rel)
        ):
            skipped_files += 1
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            continue
        updated = original
        file_hits: list[str] = []
        for old, new in replacements:
            if ignore.patterns:
                updated, count = exact_replace_respecting_ignore(
                    updated, old, new, ignored_patterns=ignore.patterns
                )
            else:
                updated, count = exact_replace(updated, old, new)
            if count:
                match_counts[old] = match_counts.get(old, 0) + count
                file_hits.append(f"{old!r} -> {new!r} ({count}x)")
        for rule in drop_rules:
            raw_values = rule.get("values")
            values = list(raw_values) if isinstance(raw_values, list) else None
            param = str(rule.get("param") or rule.get("old_param") or "")
            if not param:
                continue
            updated, count = apply_param_drop(
                path,
                updated,
                function_target=str(rule.get("function_target") or ""),
                param=param,
                values=values,
            )
            if count:
                file_hits.append(f"drop {param!r} ({count}x)")
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(rel)
            for hit in file_hits:
                details.append(f"{rel}: {hit}")

    if skipped_files:
        details.append(f"ignored {skipped_files} file(s) via ignore list")

    if not changed:
        if not replacements and not drop_rules:
            details.append(
                "no EXACT_STRING_REPLACE / AST_PARAM_RENAME / AST_PARAM_DROP "
                "rules available for heuristics"
            )
        else:
            for old, new in replacements:
                if match_counts.get(old, 0) == 0:
                    details.append(
                        f"no remaining occurrences of {old!r} "
                        f"(already migrated to {new!r}, or never present)"
                    )

    return FixAttempt(strategy="heuristic", files=changed, details=details)


def _llm_suggest_fixes(
    *,
    root: Path,
    test_result: TestResult,
    packet: dict[str, Any],
    files: dict[str, str],
    ignore: IgnoreList | None = None,
    seed_urls: list[str] | None = None,
    suggested_queries: list[str] | None = None,
    log: LogFn | None = None,
    nudge: str | None = None,
    source: dict[str, Any] | None = None,
    coverage_missed: list[dict[str, Any]] | None = None,
    audit_log: MigrationAuditLog | None = None,
    attempt: int | None = None,
    file_windows: list[dict[str, Any]] | None = None,
    path_allowlist: set[str] | None = None,
    seeded_paths: list[str] | None = None,
    leftover_files: list[dict[str, Any]] | None = None,
) -> LlmRepairSuggestion:
    emit = log or _noop_log
    client = attach_llm_log(
        get_llm_client(), emit if emit is not _noop_log else None
    )
    if client is None:
        return LlmRepairSuggestion()

    from conduit.llm.executors import RepoToolExecutor
    from conduit.llm.tools import (
        agent_tools,
        resolve_max_turns,
        resolve_reasoning_effort,
    )

    ignore = ignore or IgnoreList()
    files = {k: v for k, v in files.items() if not ignore.path_ignored(k)}
    windows = [
        w
        for w in (file_windows or [])
        if isinstance(w, dict) and not ignore.path_ignored(str(w.get("path") or ""))
    ]
    allow = set(path_allowlist or files.keys())
    allow = {p for p in allow if not ignore.path_ignored(p)}
    # Always allow reading anything already seeded as a window/path.
    allow.update(files.keys())
    allow.update(str(w.get("path") or "") for w in windows)
    leftovers = [
        item
        for item in (leftover_files or [])
        if isinstance(item, dict)
        and not ignore.path_ignored(str(item.get("path") or ""))
    ]
    for item in leftovers:
        rel = str(item.get("path") or "").replace("\\", "/")
        if rel:
            allow.add(rel)
    allow.discard("")

    def _reject(rel: str, contents: str) -> str | None:
        reason = reject_self_correct_write(
            rel, contents, packet=packet, ignore=ignore, root=root
        )
        if reason and audit_log is not None:
            audit_log.record_reject(
                rel, reason, source="repair_tool", attempt=attempt
            )
        return reason

    executor = RepoToolExecutor(
        root=root,
        ignore=ignore,
        allow_writes=True,
        allow_run_tests=True,
        allow_shell=True,
        log=emit if emit is not _noop_log else _noop_log,
        reject_write=_reject,
        path_allowlist=allow or None,
    )

    failing_hint = list(seeded_paths or sorted(files.keys()))
    failing_hint = [p for p in failing_hint if not ignore.path_ignored(p)]
    for item in leftovers:
        rel = str(item.get("path") or "").replace("\\", "/")
        if rel and rel not in failing_hint:
            failing_hint.append(rel)
    structured = _structured_failure(test_result)
    journal = (
        audit_log.repair_journal(attempt=attempt)
        if audit_log is not None
        else {"entries": []}
    )
    leftover_block = ""
    if leftovers:
        leftover_block = (
            "0) leftover_files is AUTHORITATIVE from the leftover oracle. "
            "Your FIRST tool actions must read_file then write_file those paths "
            "(replace legacy tokens with packet successors). Do NOT inventory "
            "with grep; do NOT use run_shell to read/write files; do NOT rewrite "
            "consumer tests to expect new tokens while leaving configs/scripts "
            "unmigrated.\n"
        )
    prompt = {
        "instructions": (
            "Tests failed after an automatic API migration. Fix IMPLEMENTATION "
            "code so tests pass by rewriting call sites and manifests to the "
            "packet's new API. The packet may be incomplete — you may also "
            "update packet_patch.\n"
            "Edit-first workflow (do this in order):\n"
            + leftover_block
            + "1) Act on seeded_paths / file_windows / leftover_files first — "
            "those are the failing spans. Use read_file only when you need more "
            "than the window.\n"
            "2) Do NOT inventory the repo. list_files is unavailable. Prefer "
            "grep/read on allowlisted paths only (and only after editing "
            "leftovers if leftover_files is set).\n"
            "3) Make the smallest write_file that fixes the failure, then "
            "call run_tests with optional nodeids from failed_nodes for a "
            "focused retest. Full suite still runs after your turn.\n"
            "4) Consult repair_journal so you do not repeat rejected writes "
            "or restored diffs.\n"
            "5) Use web_search / fetch_url only when docs are needed for an "
            "API successor — do not guess undocumented APIs.\n"
            "6) Preserve existing public names (module-level def/class and "
            "__all__) unless tests require a rename.\n"
            "7) run_shell is only for pytest / pip show|list / tiny read-only "
            "SDK probes (import + version). Never Path/open/write/exec via shell.\n"
            "When finished, return JSON with any of:\n"
            '  "files": {"relative/path.py": "full new file contents"},\n'
            '  "packet_patch": {\n'
            '      "rules": [/* full rule objects to add or replace by match */],\n'
            '      "notes": "why the packet changed",\n'
            '      "sources": [{"url": "...", "kind": "docs"|"other"}]\n'
            "  },\n"
            '  "search_queries": ["only if tools cannot gather enough evidence"]\n'
            "Rules:\n"
            "- Prefer grounded facts from tool results / docs.\n"
            "- Every new/changed rule should include a short 'reason'.\n"
            "- Only include files that need changes (or rely on write_file tool).\n"
            "- Empty files in the final JSON is OK if write_file already saved edits.\n"
            "- Do NOT modify ignored paths, vendor decoys, or tests/test_conduit_oracle.py.\n"
            "- Do NOT split/join/concat strings to hide leftover tokens "
            "(e.g. ''.join(['a','da'])). Migrate the API instead.\n"
            "- Do NOT swallow exceptions with `except Exception: return dummy`.\n"
            "- Do NOT add unused MIGRATION_MARKERS / token tuples to satisfy smoke tests.\n"
            "- Do NOT weaken consumer tests with skip/xfail or split assertion needles.\n"
            "- Do NOT rewrite ignored patterns when they appear as LEGACY_/FORBIDDEN_/"
            "EXPECTED_/ALLOWED_ contract constants — those define the migration oracle.\n"
            "- Keep the official "
            + str(packet.get("package") or "the packet package")
            + " SDK at to_version. Migrate call sites only. "
            "Do not replace the SDK with requests/fetch or a fake/stub client.\n"
            "- Do NOT catch HTTP errors and return canned chat/embeddings.\n"
            "- Do NOT edit packets/, .conduit/, vendor/, or *.jsonl knowledge seeds.\n"
            "- Do NOT patch Path.read_text / open / sitecustomize to rewrite leftover-oracle "
            "file contents (no __LEGACY__ sanitizers or _test_shim installers).\n"
            "- Do NOT monkeypatch the SDK module (e.g. openai.chat = Compat…) to preserve "
            "0.x prompt=/dict shapes. Migrate to messages= and real SDK objects/attrs.\n"
            "- Prefer focused run_tests(nodeids=failed_nodes) before finalizing."
        ),
        "seeded_paths": failing_hint,
        "file_windows": windows,
        "leftover_files": leftovers,
        "structured_failure": structured,
        "failed_nodes": structured["failed_nodes"],
        "leftover_tokens": structured["leftover_tokens"],
        "exception_snippets": structured["exception_snippets"],
        "failure_digest": structured["failure_digest"],
        "failure_fingerprint": structured["failure_fingerprint"],
        "repair_journal": journal,
        "path_allowlist": sorted(allow),
        "nudge": nudge or "",
        "ignore": ignore.to_prompt_dict(),
        "error_stdout": _pack_stream(test_result.stdout or ""),
        "error_stderr": _pack_stream(test_result.stderr or ""),
        "packet": _packet_for_prompt(packet),
        "source": _source_for_prompt(source),
        "coverage_missed": list(coverage_missed or []),
        "files": files,
        "seed_urls": list(seed_urls or [])[:20],
        "suggested_queries": list(suggested_queries or [])[:12],
    }
    system = (
        "You are a migration repair agent with scoped local tools "
        "(read/grep/write, focused run_tests, tightly allowlisted run_shell) plus "
        "web_search/fetch_url. Edit leftover_files and failing spans first. "
        "No repo inventory. No shell file IO. Preserve public names. "
        "Reply with a final JSON object only. Honor ignore list and path_allowlist. "
        "Update packet_patch when the migration packet must change."
    )
    try:
        run_agent = getattr(client, "run_agent", None)
        if callable(run_agent):
            max_turns = resolve_max_turns(32)
            emit(
                f"[self-correct] LLM repair "
                f"(effort={resolve_reasoning_effort()}, max_turns={max_turns})…"
            )
            data = run_agent(
                system=system,
                user=json.dumps(prompt),
                tools=agent_tools(mode="self_correct"),
                tool_executor=executor,
                max_turns=max_turns,
            )
        else:
            emit(
                f"[self-correct] LLM repair "
                f"(effort={resolve_reasoning_effort()}, one-shot)…"
            )
            data = client.complete_json(system=system, user=json.dumps(prompt))
    except Exception as exc:
        emit(f"[self-correct] LLM repair failed: {exc}")
        return LlmRepairSuggestion()

    if not isinstance(data, dict):
        return LlmRepairSuggestion()

    files_out = data.get("files") or {}
    updates = {
        str(k): str(v)
        for k, v in files_out.items()
        if isinstance(v, str) and not ignore.path_ignored(str(k))
    }
    # Files already written via tools also count as repairs
    for rel in executor.written_files:
        if rel not in updates and not ignore.path_ignored(rel):
            try:
                updates[rel] = (root / rel).read_text(encoding="utf-8")
            except OSError:
                updates.setdefault(rel, "")
    queries: list[str] = []
    for q in data.get("search_queries") or []:
        if isinstance(q, str) and q.strip():
            queries.append(q.strip())
    patch = data.get("packet_patch")
    if not isinstance(patch, dict):
        patch = {}
    return LlmRepairSuggestion(
        files=updates,
        search_queries=queries[:8],
        packet_patch=patch,
        snapshots=dict(executor.snapshots),
    )


def _evidence_url_note(evidence: str, *, limit: int = 5) -> str:
    if not evidence:
        return ""
    urls = re.findall(r"https?://\S+", evidence)
    uniq: list[str] = []
    seen: set[str] = set()
    for u in urls:
        u = u.rstrip(")")
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
        if len(uniq) >= limit:
            break
    return (" Evidence: " + ", ".join(uniq)) if uniq else ""


def _run_verified_tests(
    root: Path,
    packet: dict[str, Any],
    *,
    scan_files: list[str] | None = None,
    llm_audit: bool = False,
    log: LogFn | None = None,
    audit_log: MigrationAuditLog | None = None,
) -> TestResult:
    result = run_tests(root)
    if not result.passed:
        return result
    report = run_anticheat(
        root,
        packet,
        scan_files,
        llm=llm_audit,
        log=log,
        audit_log=audit_log,
    )
    if report.findings:
        return anticheat_failure_result(report.findings, source=report.source)
    return result


def verify_with_self_correct(
    root: Path,
    packet: dict[str, Any],
    *,
    max_retries: int = 5,
    verbose: bool = False,
    log: LogFn | None = None,
    source: dict[str, Any] | None = None,
    coverage_missed: list[dict[str, Any]] | None = None,
    audit_log: MigrationAuditLog | None = None,
) -> tuple[TestResult, list[str]]:
    """Run tests; on failure, research + LLM/heuristic-fix and retry (default 5)."""
    emit: LogFn = log or print
    vlog: LogFn = emit if verbose else _noop_log

    if audit_log is None:
        audit_log = MigrationAuditLog.from_packet(packet, root=root)

    corrected_files: list[str] = []
    mech = run_anticheat(
        root, packet, llm=False, log=vlog, audit_log=audit_log
    )
    if mech.findings:
        result = anticheat_failure_result(mech.findings, source=mech.source)
    else:
        result = _run_verified_tests(
            root, packet, llm_audit=True, log=emit, audit_log=audit_log
        )
    try:
        audit_log.persist(root)
    except OSError:
        pass
    if result.passed:
        return result, corrected_files

    if _is_anticheat_failure(result):
        _emit_unpassable_stop(emit, "anticheat failure (not retryable)")
        return _mark_unpassable(
            result, "anticheat failure (not retryable)"
        ), corrected_files

    source = source or _load_source_packet(root, packet)
    ignore = build_ignore_list(root, packet)
    if verbose and (ignore.paths or ignore.globs or ignore.patterns):
        vlog(
            "[self-correct] ignore list: "
            f"paths={sorted(ignore.paths) or '[]'} "
            f"globs={ignore.globs or []} "
            f"patterns={len(ignore.patterns)}"
        )

    evidence = ""
    pending_queries: list[str] = []
    empty_nudge_used = False
    pending_nudge: str | None = None
    prev_fingerprint = _failure_fingerprint(result)
    consecutive_restores = 0

    for attempt in range(1, max_retries + 1):
        emit(f"[self-correct] attempt {attempt}/{max_retries} after test failure")
        nodes = _failed_nodes(result)
        leftovers = _leftover_tokens(result)
        digest = build_failure_digest(result)
        if nodes:
            shown = ", ".join(nodes[:3])
            if len(nodes) > 3:
                shown += f" (+{len(nodes) - 3} more)"
            emit(f"[self-correct] failing: {shown}")
        elif leftovers:
            shown = ", ".join(leftovers[:4])
            if len(leftovers) > 4:
                shown += f" (+{len(leftovers) - 4} more)"
            emit(f"[self-correct] leftovers: {shown}")
        elif digest["exception_snippets"]:
            emit(f"[self-correct] failure: {digest['exception_snippets'][0][:160]}")
        elif digest["text"]:
            emit(f"[self-correct] failure: {digest['text'][:160]}")
        else:
            reason = (result.fail_reason or result.summary or "unknown failure").strip()
            emit(f"[self-correct] failure: {reason[:120]}")
        if digest["text"]:
            vlog(f"[self-correct] failure digest:\n{digest['text']}")
        vlog(f"[self-correct] failure summary:\n{_failure_excerpt(result)}")

        repair_ctx = collect_repair_context(
            root, result, packet=packet, source=source
        )
        context_files = {
            k: v
            for k, v in repair_ctx.files.items()
            if not ignore.path_ignored(k)
        }
        allowlist = {
            p for p in repair_ctx.allowlist if not ignore.path_ignored(p)
        }
        emit(
            f"[self-correct] seeded {len(context_files)} span(s), "
            f"allowlist={len(allowlist)}"
            + (
                f", leftovers={len(repair_ctx.leftover_files)}"
                if repair_ctx.leftover_files
                else ""
            )
        )
        vlog(
            f"[self-correct] context windows for repair: "
            f"{', '.join(sorted(context_files)) or '(none)'} "
            f"(allowlist={len(allowlist)})"
        )

        from conduit.packet.failure_rules import suggest_rules_from_failure

        failure_rules = suggest_rules_from_failure(
            result,
            file_windows=repair_ctx.file_windows,
            packet=packet,
        )
        if failure_rules:
            patch_details = _apply_packet_patch(packet, {"rules": failure_rules})
            emit(
                f"[self-correct] verify-learned {len(failure_rules)} rule(s): "
                + ", ".join(
                    f"drop {r.get('param')!r}"
                    + (
                        f"={r.get('values')}"
                        if r.get("values") is not None
                        else ""
                    )
                    for r in failure_rules[:3]
                )
                + ("…" if len(failure_rules) > 3 else "")
            )
            vlog(
                "[self-correct] failure-learned rules: "
                + "; ".join(patch_details or ["merged"])
            )
            if audit_log is not None:
                audit_log.record_packet_patch(patch_details, attempt=attempt)
            learned = _heuristic_fix(root, packet, ignore)
            if learned.files:
                corrected_files.extend(learned.files)
                emit(
                    f"[self-correct] applied verify-learned rules to "
                    f"{len(learned.files)} file(s)"
                )

        nudge: str | None = pending_nudge
        pending_nudge = None
        if empty_nudge_used and not nudge:
            leftover_hint = (
                f" leftover_files={[x.get('path') for x in repair_ctx.leftover_files[:8]]};"
                if repair_ctx.leftover_files
                else ""
            )
            nudge = (
                "Previous attempt made no file edits. You MUST edit "
                "seeded_paths / file_windows / leftover_files with write_file first "
                f"(seeded_paths={sorted(context_files.keys())};{leftover_hint}), "
                "then run_tests(nodeids=failed_nodes) before finishing."
            )

        suggestion = LlmRepairSuggestion()
        if get_llm_client() is not None:
            seeds, queries = _extract_research_targets(
                result, packet, extra_queries=pending_queries, source=source
            )
            pending_queries = []
            vlog(
                "[self-correct] agent seeds/queries: "
                f"{len(seeds)} URL(s), {len(queries)} quer(ies) "
                "(model may web_search / fetch_url)"
            )

            suggestion = _llm_suggest_fixes(
                root=root,
                test_result=result,
                packet=packet,
                files=context_files,
                ignore=ignore,
                seed_urls=seeds,
                suggested_queries=queries,
                log=emit,
                nudge=nudge,
                source=source,
                coverage_missed=coverage_missed,
                audit_log=audit_log,
                attempt=attempt,
                file_windows=repair_ctx.file_windows,
                path_allowlist=allowlist,
                seeded_paths=repair_ctx.seeded_paths,
                leftover_files=repair_ctx.leftover_files,
            )

            # Fallback providers without tools may still return search_queries.
            if suggestion.search_queries and not suggestion.files:
                vlog(
                    "[self-correct] LLM requested more search: "
                    + "; ".join(suggestion.search_queries)
                )
                more, _w2 = _research_for_failure(
                    result,
                    packet,
                    log=vlog,
                    extra_queries=suggestion.search_queries,
                )
                if more:
                    evidence = more[-28000:]
                    # Re-ask with fetched evidence stuffed into suggested context
                    context_files = dict(context_files)
                    suggestion = _llm_suggest_fixes(
                        root=root,
                        test_result=result,
                        packet=packet,
                        files={
                            **context_files,
                            "(research_notes.txt)": evidence,
                        },
                        ignore=ignore,
                        seed_urls=seeds,
                        suggested_queries=suggestion.search_queries,
                        log=emit,
                        nudge=nudge,
                        source=source,
                        coverage_missed=coverage_missed,
                        audit_log=audit_log,
                        attempt=attempt,
                        file_windows=repair_ctx.file_windows,
                        path_allowlist=allowlist,
                        seeded_paths=repair_ctx.seeded_paths,
                        leftover_files=repair_ctx.leftover_files,
                    )
                if suggestion.search_queries and not suggestion.files:
                    pending_queries = list(suggestion.search_queries)

        patch_details: list[str] = []
        if suggestion.packet_patch:
            patch_details = _apply_packet_patch(packet, suggestion.packet_patch)
            if patch_details:
                vlog(
                    "[self-correct] packet updated: " + "; ".join(patch_details)
                )
                audit_log.record_packet_patch(patch_details, attempt=attempt)

        snapshots = dict(suggestion.snapshots)
        if suggestion.files:
            changed = _apply_file_updates(
                root,
                suggestion.files,
                snapshots,
                packet=packet,
                ignore=ignore,
                log=emit,
                audit_log=audit_log,
                attempt=attempt,
            )
            if not changed and not patch_details:
                _emit_unpassable_stop(emit, "all repair writes rejected")
                result = _mark_unpassable(result, "all repair writes rejected")
                break
            fix = FixAttempt(
                strategy="llm",
                files=changed,
                details=[f"{rel}: rewritten by LLM" for rel in changed]
                + [f"packet: {d}" for d in patch_details],
            )
            if changed or patch_details:
                _append_packet_note(
                    packet,
                    f"- Self-correct (attempt {attempt}): LLM repaired "
                    f"{', '.join(changed) or '(packet only)'}."
                    f"{_evidence_url_note(evidence)}",
                )
        else:
            if get_llm_client() is None:
                vlog("[self-correct] no LLM configured; applying packet heuristic fixes")
            elif suggestion.search_queries:
                vlog(
                    "[self-correct] LLM still needs research; "
                    "applying heuristic fixes this round"
                )
            else:
                vlog(
                    "[self-correct] LLM returned no file updates; "
                    "applying heuristic fixes"
                )
            fix = _heuristic_fix(root, packet, ignore=ignore)
            if fix.files:
                _append_packet_note(
                    packet,
                    f"- Self-correct (attempt {attempt}): heuristic packet rules "
                    f"updated {', '.join(fix.files)}.",
                )
            # Packet-only LLM update counts as progress even without file edits
            if not fix.files and patch_details:
                fix = FixAttempt(
                    strategy="llm",
                    files=[],
                    details=[f"packet: {d}" for d in patch_details],
                )
                # Re-run tests after packet-only change won't help files, but
                # keep going so a later attempt can use the updated packet.
                # Mark synthetic progress via details; avoid early-stop.
                fix.files = ["(packet)"]

        corrected_files.extend(f for f in fix.files if f != "(packet)")
        if fix.files:
            updated = [f for f in fix.files if f != "(packet)"]
            if updated:
                emit(
                    f"[self-correct] {fix.strategy}: updated "
                    f"{len(updated)} file(s): {', '.join(updated[:5])}"
                    + ("…" if len(updated) > 5 else "")
                )
            else:
                emit(f"[self-correct] {fix.strategy}: packet patch only")
            for detail in fix.details:
                vlog(f"[self-correct]   {detail}")
        else:
            # Still have pending search for next attempt — don't stop early.
            if pending_queries and get_llm_client() is not None and attempt < max_retries:
                emit(
                    "[self-correct] no file edits yet; "
                    "will continue with LLM-requested search next attempt"
                )
                continue
            # One nudge retry when LLM is configured and produced nothing.
            if (
                get_llm_client() is not None
                and not empty_nudge_used
                and attempt < max_retries
            ):
                empty_nudge_used = True
                emit(
                    "[self-correct] no file changes; nudging LLM once with "
                    "explicit failing-path instructions"
                )
                for detail in fix.details:
                    vlog(f"[self-correct]   {detail}")
                continue
            emit(
                f"[self-correct] strategy={fix.strategy}; "
                "no file changes produced this attempt"
            )
            for detail in fix.details:
                vlog(f"[self-correct]   {detail}")
            emit(
                "[self-correct] stopping early: automatic repair made no edits "
                "(configure an LLM for deeper fixes, or resolve remaining failures manually)"
            )
            break

        previous = result
        emit("[self-correct] re-running full test suite…")
        result = _run_verified_tests(
            root, packet, llm_audit=True, log=emit, audit_log=audit_log
        )
        try:
            audit_log.persist(root)
        except OSError:
            pass
        if result.passed:
            emit(f"[self-correct] tests passed after attempt {attempt}")
            return result, sorted(set(corrected_files))

        if _is_anticheat_failure(result):
            _emit_unpassable_stop(emit, "anticheat failure (not retryable)")
            result = _mark_unpassable(result, "anticheat failure (not retryable)")
            break

        if snapshots and _repair_regressed(previous, result):
            restored = _restore_snapshots(root, snapshots)
            if restored:
                audit_log.record_restore(restored, attempt=attempt)
            lost_bits: list[str] = []
            for rel, original in snapshots.items():
                if not original:
                    continue
                lost = _top_level_names(original) - _top_level_names(
                    suggestion.files.get(rel) or ""
                )
                if lost:
                    lost_bits.append(f"{rel} dropped {sorted(lost)}")
            emit(
                "[self-correct] repair regressed tests; restored "
                f"{len(restored)} file(s)"
                + (f" ({'; '.join(lost_bits[:4])})" if lost_bits else "")
            )
            consecutive_restores += 1
            result = _run_verified_tests(
                root, packet, llm_audit=True, log=emit, audit_log=audit_log
            )
            try:
                audit_log.persist(root)
            except OSError:
                pass
            empty_nudge_used = False
            pending_nudge = (
                "Previous write regressed tests (collection/import failure). "
                "Original files were restored. Preserve public names. "
                + (" ".join(lost_bits[:6]) if lost_bits else "")
            )
            if result.passed:
                emit("[self-correct] tests passed after restoring snapshot")
                return result, sorted(set(corrected_files))
            if _is_anticheat_failure(result):
                _emit_unpassable_stop(emit, "anticheat failure (not retryable)")
                result = _mark_unpassable(
                    result, "anticheat failure (not retryable)"
                )
                break
            if consecutive_restores >= 2:
                _emit_unpassable_stop(emit, "repair regressed twice")
                result = _mark_unpassable(result, "repair regressed twice")
                break
            # Post-restore baseline for stagnant detection.
            prev_fingerprint = _failure_fingerprint(result)
            emit(
                f"[self-correct] still failing after attempt {attempt}: "
                f"{(result.fail_reason or result.summary or '')[:100]}"
            )
            vlog(
                f"[self-correct] still failing after attempt {attempt}: "
                f"{result.summary}"
            )
            if suggestion.search_queries:
                pending_queries = list(
                    dict.fromkeys([*pending_queries, *suggestion.search_queries])
                )
            continue

        consecutive_restores = 0
        fp = _failure_fingerprint(result)
        if fp == prev_fingerprint:
            _emit_unpassable_stop(
                emit, "no progress (same failures for 2 attempts)"
            )
            result = _mark_unpassable(
                result, "no progress (same failures for 2 attempts)"
            )
            break
        prev_fingerprint = fp
        emit(
            f"[self-correct] still failing after attempt {attempt}: "
            f"{(result.fail_reason or result.summary or '')[:100]}"
        )
        vlog(f"[self-correct] still failing after attempt {attempt}: {result.summary}")
        # Next attempt should research again with the new failure signature
        if suggestion.search_queries:
            pending_queries = list(
                dict.fromkeys([*pending_queries, *suggestion.search_queries])
            )

    try:
        audit_log.persist(root)
    except OSError:
        pass
    return result, sorted(set(corrected_files))
