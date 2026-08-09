"""LLM self-correction loop after packet apply (tests + production files)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from conduit.llm import get_llm_client
from conduit.repair_ignore import IgnoreList, build_ignore_list
from conduit.test_runner import TestResult, run_tests

LogFn = Callable[[str], None]

_MODEL_ID_RE = re.compile(
    r"\b("
    r"gpt-[a-z0-9._-]+"
    r"|o[0-9][a-z0-9._-]*"
    r"|text-embedding-[a-z0-9._-]+"
    r"|text-moderation-[a-z0-9._-]+"
    r"|whisper-[a-z0-9._-]+"
    r"|tts-[a-z0-9._-]+"
    r"|dall-e-[0-9]"
    r"|chatgpt-[a-z0-9._-]+"
    r"|omni-moderation(?:-[a-z0-9._-]+)?"
    r")\b",
    re.I,
)
_PATH_RE = re.compile(r"/v1/[a-z0-9/_-]+", re.I)


def _noop_log(_: str) -> None:
    return None


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


def _paths_from_traceback(root: Path, text: str, limit: int = 12) -> list[Path]:
    found: list[Path] = []
    for m in re.finditer(r'File "([^"]+)"', text):
        raw = m.group(1)
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        if path.is_file() and str(path.resolve()).startswith(str(root.resolve())):
            if path not in found:
                found.append(path)
        if len(found) >= limit:
            break
    return found


def _collect_context_files(root: Path, test_result: TestResult, limit: int = 12) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in _paths_from_traceback(
        root, (test_result.stdout or "") + "\n" + (test_result.stderr or "")
    ):
        try:
            files[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
        except OSError:
            continue

    candidates = list((root / "tests").rglob("*.py")) if (root / "tests").is_dir() else []
    candidates += list(root.glob("test_*.py"))
    src = root / "src"
    if src.is_dir():
        candidates += list(src.rglob("*.py"))[:20]
    for path in candidates:
        if len(files) >= limit:
            break
        rel = str(path.relative_to(root))
        if rel in files:
            continue
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return files


def _apply_file_updates(root: Path, updates: dict[str, str]) -> list[str]:
    changed: list[str] = []
    root_resolved = root.resolve()
    for rel, content in updates.items():
        path = (root / rel).resolve()
        if not str(path).startswith(str(root_resolved)):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        changed.append(rel)
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
                val = str(rule.get(key) or "")
                if _MODEL_ID_RE.fullmatch(val) or _MODEL_ID_RE.search(val):
                    ids.append(val)
    return ids


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
) -> tuple[list[str], list[str]]:
    """Return (seed_urls, search_queries) derived from failure + packet."""
    from conduit.detect.modules.openai.model_docs import (
        MODELS_CATALOG_URL,
        model_doc_url,
    )

    blob = "\n".join(
        [
            test_result.stdout or "",
            test_result.stderr or "",
            json.dumps(packet.get("rules") or [])[:4000],
        ]
    )
    model_ids = sorted({m.lower() for m in _MODEL_ID_RE.findall(blob)})
    for mid in _ids_from_packet(packet):
        model_ids.append(mid.lower())
    # preserve order unique
    seen: set[str] = set()
    ordered_ids: list[str] = []
    for mid in model_ids:
        if mid in seen:
            continue
        seen.add(mid)
        ordered_ids.append(mid)

    paths = sorted({p.lower() for p in _PATH_RE.findall(blob)})
    package = str(packet.get("package") or "openai")

    seeds = [
        MODELS_CATALOG_URL,
        "https://developers.openai.com/api/docs/models",
        "https://platform.openai.com/docs/deprecations",
        "https://developers.openai.com/api/docs/deprecations",
    ]
    for mid in ordered_ids[:6]:
        seeds.append(model_doc_url(mid))

    queries: list[str] = [
        f"{package} API migration test failure",
        f"{package} python sdk migration breaking change",
    ]
    queries.extend(_error_search_snippets(test_result))
    for mid in ordered_ids[:4]:
        queries.append(f"{package} model {mid} supported endpoints replacement")
    for path in paths[:3]:
        queries.append(f"{package} {path} replacement deprecation")
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
    allow_hosts = [
        "platform.openai.com",
        "developers.openai.com",
        "github.com",
    ]
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

    ignore = ignore or IgnoreList()
    replacements: list[tuple[str, str]] = []
    for rule in packet.get("rules") or []:
        if rule.get("type") == "EXACT_STRING_REPLACE":
            replacements.append((str(rule["match"]), str(rule["replace"])))
        if rule.get("type") == "AST_PARAM_RENAME":
            replacements.append((str(rule["old_param"]), str(rule["new_param"])))

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
    if (root / "tests").is_dir():
        targets += list((root / "tests").rglob("*.py"))
    targets += list(root.glob("test_*.py"))
    if (root / "src").is_dir():
        targets += list((root / "src").rglob("*.py"))

    changed: list[str] = []
    details: list[str] = []
    match_counts = {old: 0 for old, _ in replacements}
    skipped_files = 0

    for path in targets:
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        if ignore.path_ignored(rel):
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
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(rel)
            for hit in file_hits:
                details.append(f"{rel}: {hit}")

    if skipped_files:
        details.append(f"ignored {skipped_files} file(s) via ignore list")

    if not changed:
        if not replacements:
            details.append(
                "no EXACT_STRING_REPLACE / AST_PARAM_RENAME rules available for heuristics"
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
    test_result: TestResult,
    packet: dict[str, Any],
    files: dict[str, str],
    ignore: IgnoreList | None = None,
    evidence: str = "",
    log: LogFn | None = None,
) -> LlmRepairSuggestion:
    emit = log or _noop_log
    client = get_llm_client()
    if client is None:
        return LlmRepairSuggestion()

    ignore = ignore or IgnoreList()
    files = {k: v for k, v in files.items() if not ignore.path_ignored(k)}

    prompt = {
        "instructions": (
            "Tests failed after an automatic API migration. Your job is to make "
            "the consumer repo pass tests.\n"
            "The migration packet may be incomplete or wrong — you may fix code "
            "AND update the packet when needed.\n"
            "You have web research evidence below. If it is NOT enough to fix the "
            "failure, request more general search (do not guess).\n"
            "Return JSON with any of:\n"
            '  "files": {"relative/path.py": "full new file contents"},\n'
            '  "packet_patch": {\n'
            '      "rules": [/* full rule objects to add or replace by match */],\n'
            '      "notes": "why the packet changed",\n'
            '      "sources": [{"url": "...", "kind": "docs"|"other"}]\n'
            "  },\n"
            '  "search_queries": ["web search queries to run next if evidence is insufficient"]\n'
            "Rules:\n"
            "- Prefer grounded facts from evidence; when requesting search_queries, "
            "be specific (error text, model id, endpoint, SDK version).\n"
            "- Every new/changed rule should include a short 'reason'.\n"
            "- Only include files that need changes.\n"
            "- Do NOT modify ignored paths.\n"
            "- Do NOT rewrite ignored patterns when they appear as LEGACY_/FORBIDDEN_/"
            "EXPECTED_/ALLOWED_ contract constants — those define the migration oracle.\n"
            "- If you can fix with current evidence, return files (and packet_patch if "
            "the packet should change). If you cannot, return search_queries and "
            "omit files (or leave files empty)."
        ),
        "ignore": ignore.to_prompt_dict(),
        "error_stdout": test_result.stdout[-6000:],
        "error_stderr": test_result.stderr[-6000:],
        "packet": packet,
        "files": files,
        "evidence": evidence[:28000] if evidence else "",
    }
    try:
        data = client.complete_json(
            system=(
                "You are a migration repair agent with web research. "
                "Reply with JSON only. Honor ignore list. "
                "Request search_queries when evidence is insufficient; "
                "update packet_patch when the migration packet must change."
            ),
            user=json.dumps(prompt),
        )
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


def verify_with_self_correct(
    root: Path,
    packet: dict[str, Any],
    *,
    max_retries: int = 5,
    verbose: bool = False,
    log: LogFn | None = None,
) -> tuple[TestResult, list[str]]:
    """Run tests; on failure, research + LLM/heuristic-fix and retry (default 5)."""
    emit: LogFn = log or print
    vlog: LogFn = emit if verbose else _noop_log

    corrected_files: list[str] = []
    result = run_tests(root)
    if result.passed:
        return result, corrected_files

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

    for attempt in range(1, max_retries + 1):
        emit(f"[self-correct] attempt {attempt}/{max_retries} after test failure")
        vlog(f"[self-correct] failure summary:\n{_failure_excerpt(result)}")

        context_files = _collect_context_files(root, result)
        context_files = {
            k: v for k, v in context_files.items() if not ignore.path_ignored(k)
        }
        vlog(
            f"[self-correct] context files for repair: "
            f"{', '.join(sorted(context_files)) or '(none)'}"
        )

        suggestion = LlmRepairSuggestion()
        if get_llm_client() is not None:
            # Fresh research each attempt (failure text / packet may have changed),
            # plus any LLM-requested queries from the previous round.
            evidence, _warnings = _research_for_failure(
                result,
                packet,
                log=vlog,
                extra_queries=pending_queries,
            )
            pending_queries = []

            suggestion = _llm_suggest_fixes(
                test_result=result,
                packet=packet,
                files=context_files,
                ignore=ignore,
                evidence=evidence,
                log=vlog,
            )

            # Evidence was insufficient — run the model's search queries and ask again.
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
                    evidence = (evidence + "\n\n" + more)[-28000:]
                suggestion = _llm_suggest_fixes(
                    test_result=result,
                    packet=packet,
                    files=context_files,
                    ignore=ignore,
                    evidence=evidence,
                    log=vlog,
                )
                # Carry leftover search needs into the next attempt if still stuck
                if suggestion.search_queries and not suggestion.files:
                    pending_queries = list(suggestion.search_queries)

        patch_details: list[str] = []
        if suggestion.packet_patch:
            patch_details = _apply_packet_patch(packet, suggestion.packet_patch)
            if patch_details:
                vlog(
                    "[self-correct] packet updated: " + "; ".join(patch_details)
                )

        if suggestion.files:
            changed = _apply_file_updates(root, suggestion.files)
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
            vlog(
                f"[self-correct] strategy={fix.strategy}; "
                f"updated {len([f for f in fix.files if f != '(packet)'])} file(s)"
                + (
                    f": {', '.join(f for f in fix.files if f != '(packet)')}"
                    if any(f != "(packet)" for f in fix.files)
                    else " (packet patch only)"
                )
            )
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

        result = run_tests(root)
        if result.passed:
            vlog(f"[self-correct] tests passed after attempt {attempt}")
            return result, sorted(set(corrected_files))
        vlog(f"[self-correct] still failing after attempt {attempt}: {result.summary}")
        # Next attempt should research again with the new failure signature
        if suggestion.search_queries:
            pending_queries = list(
                dict.fromkeys([*pending_queries, *suggestion.search_queries])
            )

    return result, sorted(set(corrected_files))
