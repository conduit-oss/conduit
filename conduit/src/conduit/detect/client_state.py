"""Client package state: generic pre-step before vendor detect modules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conduit.prune.grep_imports import SCAN_SUFFIXES, SKIP_DIRS, prune_by_imports

# Vendor-specific token packs. Framework is generic; patterns are not.
_OPENAI_MODEL_FIND_RE = re.compile(
    r"(?:ft-)?"
    r"(?:"
    r"gpt-[a-z0-9._-]+"
    r"|o[0-9][a-z0-9._-]*"
    r"|dall-e-[0-9]"
    r"|chatgpt-[a-z0-9._-]+"
    r"|text-embedding-[a-z0-9._-]+"
    r"|text-similarity-[a-z0-9._-]+"
    r"|text-moderation-[a-z0-9._-]+"
    r"|whisper-[a-z0-9._-]+"
    r"|tts-[a-z0-9._-]+"
    r"|gpt-image-[a-z0-9._-]+"
    r"|codex-[a-z0-9._-]+"
    r"|omni-moderation(?:-[a-z0-9._-]+)?"
    r"|computer-use-[a-z0-9._-]+"
    r")",
    re.IGNORECASE,
)

_OPENAI_API_FIND_RE = re.compile(
    r"(?:ChatCompletion|/v1/[a-z0-9/_-]+|chat\.completions|"
    r"Completion\.create|embeddings\.create)",
    re.IGNORECASE,
)

_DOCISH_PARTS = {
    "docs",
    "doc",
    "changelog",
    "changelogs",
    "examples",
    "example",
    "vendor",
    "third_party",
}

PACKAGE_PATTERN_PACKS: dict[str, dict[str, re.Pattern[str]]] = {
    "openai": {
        "model_id": _OPENAI_MODEL_FIND_RE,
        "api_pattern": _OPENAI_API_FIND_RE,
    },
}


def pattern_pack_for(package: str) -> dict[str, re.Pattern[str]]:
    """Pattern pack from the vendor profile, falling back to built-in openai pack."""
    try:
        from conduit.detect.vendor_profile import profile_for_package

        prof = profile_for_package(package)
        if prof is not None:
            pack = prof.pattern_pack()
            if pack:
                return pack
    except Exception:
        pass
    return PACKAGE_PATTERN_PACKS.get(package.lower(), {})


@dataclass
class PackageClientState:
    """Where the client repo stands for one dependency package."""

    package: str
    installed_version: str | None = None
    model_ids: list[str] = field(default_factory=list)
    import_files: list[str] = field(default_factory=list)
    api_patterns: list[str] = field(default_factory=list)
    usages: list[dict[str, Any]] = field(default_factory=list)
    ecosystems: list[str] = field(default_factory=list)
    source: str = "regex"  # regex | regex+llm | agent | demo
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "installed_version": self.installed_version,
            "model_ids": list(self.model_ids),
            "import_files": list(self.import_files),
            "api_patterns": list(self.api_patterns),
            "usages": [dict(u) for u in self.usages],
            "ecosystems": list(self.ecosystems),
            "source": self.source,
            "notes": list(self.notes),
        }


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _is_docish(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    if parts & _DOCISH_PARTS:
        return True
    name = path.name.lower()
    return name in {"readme.md", "changelog.md", "history.md"}


def _detect_ecosystems(root: Path, package: str) -> list[str]:
    """Which manifests declare this package (pip vs npm)."""
    found: list[str] = []
    pkg_l = package.lower()
    for name, eco in (
        ("requirements.txt", "pip"),
        ("pyproject.toml", "pip"),
        ("package.json", "npm"),
    ):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeDecodeError):
            continue
        if pkg_l in text:
            if eco not in found:
                found.append(eco)
    return found


def _extract_tokens(text: str, pattern: re.Pattern[str]) -> list[str]:
    hits = {m.group(0) for m in pattern.finditer(text)}
    return sorted(hits)


def _regex_scan_package(
    root: Path,
    package: str,
    *,
    installed: dict[str, str],
    demo: bool = False,
) -> PackageClientState:
    packs = pattern_pack_for(package)
    model_re = packs.get("model_id")
    api_re = packs.get("api_pattern")

    files = prune_by_imports(root, [package])
    # Also scan common config suffixes next to import hits' trees is enough;
    # extend with .env / .yaml / .yml / .toml / .json under root (non-docish).
    config_hits: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if _is_docish(path):
            continue
        if path.suffix.lower() in {".env", ".yaml", ".yml", ".toml", ".json", ".ini"}:
            config_hits.append(path)

    scan_files = list(dict.fromkeys([*files, *config_hits]))
    model_ids: set[str] = set()
    api_patterns: set[str] = set()
    import_rels: list[str] = []

    known: set[str] = set()
    known_err: Exception | None = None
    try:
        from conduit.detect.vendor_profile import collect_known_ids, profile_for_package

        prof = profile_for_package(package)
        if prof is not None and prof.known_ids is not None:
            known = collect_known_ids(package, demo=demo)
    except Exception as exc:  # noqa: BLE001 — fail soft; regex bootstrap below
        known = set()
        known_err = exc

    for path in scan_files:
        if _is_docish(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = _rel(path, root)
        if path.suffix.lower() in SCAN_SUFFIXES and path in files:
            import_rels.append(rel)
        if known:
            from conduit.detect.modules.openai.known_models import (
                extract_model_kwarg_ids,
                find_known_models_in_text,
            )

            model_ids.update(find_known_models_in_text(text, known))
            model_ids.update(extract_model_kwarg_ids(text, known))
            if model_re:
                known_lower = {k.lower(): k for k in known}
                for tok in _extract_tokens(text, model_re):
                    canon = known_lower.get(tok.lower())
                    if canon:
                        model_ids.add(canon)
        elif model_re:
            # Bootstrap only when the dynamic universe is unavailable.
            model_ids.update(_extract_tokens(text, model_re))
        if api_re:
            api_patterns.update(_extract_tokens(text, api_re))

    version = None
    for name, ver in installed.items():
        if name.lower() == package.lower():
            version = ver
            break

    notes: list[str] = []
    if known_err is not None:
        notes.append(f"known-model universe unavailable: {known_err}")
    if known:
        notes.append(f"model discovery grounded on {len(known)} known model ids")
    if not model_ids:
        notes.append(
            "no model ids found in import-pruned / config files "
            "(empty means unknown, not all-clear)"
        )

    return PackageClientState(
        package=package,
        installed_version=version,
        model_ids=sorted(model_ids),
        import_files=sorted(set(import_rels)),
        api_patterns=sorted(api_patterns),
        ecosystems=_detect_ecosystems(root, package),
        source="regex",
        notes=notes,
    )


def _file_corpus(files: list[Path]) -> str:
    parts: list[str] = []
    for path in files:
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(parts)


def _token_in_corpus(token: str, corpus_lower: str) -> bool:
    tok = (token or "").strip()
    return bool(tok) and tok.lower() in corpus_lower


def _normalize_usage(raw: Any, *, corpus_lower: str, known_files: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    ident = str(raw.get("id") or raw.get("model_id") or "").strip()
    if not ident or not _token_in_corpus(ident, corpus_lower):
        return None
    callees = [
        str(c).strip()
        for c in (raw.get("callees") or [])
        if str(c).strip() and _token_in_corpus(str(c).strip(), corpus_lower)
    ]
    paths = [
        str(p).strip()
        for p in (raw.get("paths") or [])
        if str(p).strip() and _token_in_corpus(str(p).strip(), corpus_lower)
    ]
    files = []
    for rel in raw.get("files") or []:
        item = str(rel).strip().replace("\\", "/")
        if item and item in known_files:
            files.append(item)
    return {"id": ident, "callees": callees, "paths": paths, "files": files}


def _merge_usage_row(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key in ("callees", "paths", "files"):
        seen = {str(x) for x in existing.get(key) or []}
        for item in incoming.get(key) or []:
            if item not in seen:
                existing.setdefault(key, []).append(item)
                seen.add(str(item))


_PATH_TOKEN_RE = re.compile(r"(/[A-Za-z0-9._~/-]{2,})")
_CREATE_CALL_RE = re.compile(r"\b([A-Za-z_][\w.]*)\.create\s*\(")
_MAX_ALLOWLIST = 80
_MAX_HITS = 120
_MAX_DOSSIER_CHARS = 55_000
_SNIPPET_LEN = 120
_ENRICH_MAX_TURNS = 8


def _dossier_redact(text: str) -> str:
    try:
        from conduit.anticheat.audit_log import redact_secrets

        return redact_secrets(text)
    except ImportError:
        return text


def _snippet_at(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    snippet = text[line_start:line_end].strip()
    if len(snippet) > _SNIPPET_LEN:
        snippet = snippet[:_SNIPPET_LEN] + "…"
    return _dossier_redact(snippet)


def build_usage_dossier(
    root: Path,
    package: str,
    state: PackageClientState,
    files: list[Path],
) -> dict[str, Any]:
    """Mechanical usage dossier for LLM enrich (no LLM).

    Allowlisted paths + regex/import hit index so the agent extends coverage
    instead of rediscovering the repo.
    """
    root = root.resolve()
    pkg = (package or state.package or "").strip()
    packs = pattern_pack_for(pkg)
    model_re = packs.get("model_id")
    api_re = packs.get("api_pattern")

    # Prefer import hits, then config-ish, then other pruned files.
    import_set = {
        str(p).replace("\\", "/") for p in (state.import_files or []) if p
    }
    config_rels: list[str] = []
    other_rels: list[str] = []
    for path in files:
        if not path.is_file() or _is_docish(path):
            continue
        rel = _rel(path, root)
        if rel in import_set:
            continue
        if path.suffix.lower() in {".env", ".yaml", ".yml", ".toml", ".json", ".ini"}:
            config_rels.append(rel)
        else:
            other_rels.append(rel)

    allowlist: list[str] = []
    seen: set[str] = set()
    for rel in sorted(import_set) + sorted(set(config_rels)) + sorted(set(other_rels)):
        if rel in seen:
            continue
        seen.add(rel)
        allowlist.append(rel)
        if len(allowlist) >= _MAX_ALLOWLIST:
            break

    already_models = {str(m) for m in (state.model_ids or [])}
    already_apis = {str(a) for a in (state.api_patterns or [])}
    hits: list[dict[str, Any]] = []
    files_with_api_or_model: set[str] = set()
    files_with_import: set[str] = set()

    for rel in allowlist:
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        has_pkg = bool(re.search(rf"\b{re.escape(pkg)}\b", text, re.I)) if pkg else False
        if has_pkg or f"import {pkg}" in text or f"from {pkg}" in text:
            files_with_import.add(rel)
            # one import hit per file is enough for the index
            for m in re.finditer(
                rf"(?:import\s+{re.escape(pkg)}\b|from\s+{re.escape(pkg)}\b|"
                rf"require\(\s*['\"]{re.escape(pkg)}['\"])",
                text,
            ):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(
                    {
                        "path": rel,
                        "line": line,
                        "kind": "import",
                        "token": pkg,
                        "snippet": _snippet_at(text, m.start(), m.end()),
                    }
                )
                break

        if model_re:
            for m in model_re.finditer(text):
                tok = m.group(0)
                line = text.count("\n", 0, m.start()) + 1
                hits.append(
                    {
                        "path": rel,
                        "line": line,
                        "kind": "model",
                        "token": tok,
                        "snippet": _snippet_at(text, m.start(), m.end()),
                    }
                )
                files_with_api_or_model.add(rel)
                if len(hits) >= _MAX_HITS:
                    break
        if len(hits) >= _MAX_HITS:
            break

        if api_re:
            for m in api_re.finditer(text):
                tok = m.group(0)
                line = text.count("\n", 0, m.start()) + 1
                hits.append(
                    {
                        "path": rel,
                        "line": line,
                        "kind": "api",
                        "token": tok,
                        "snippet": _snippet_at(text, m.start(), m.end()),
                    }
                )
                files_with_api_or_model.add(rel)
                if len(hits) >= _MAX_HITS:
                    break
        if len(hits) >= _MAX_HITS:
            break

        # Generic create / path tokens only when package context is present
        if has_pkg:
            for m in _CREATE_CALL_RE.finditer(text):
                tok = f"{m.group(1)}.create"
                line = text.count("\n", 0, m.start()) + 1
                hits.append(
                    {
                        "path": rel,
                        "line": line,
                        "kind": "api",
                        "token": tok,
                        "snippet": _snippet_at(text, m.start(), m.end()),
                    }
                )
                files_with_api_or_model.add(rel)
                if len(hits) >= _MAX_HITS:
                    break
            if len(hits) >= _MAX_HITS:
                break
            for m in _PATH_TOKEN_RE.finditer(text):
                tok = m.group(1)
                if not (
                    tok.startswith("/v1/")
                    or tok.startswith("/v2/")
                    or re.match(r"^/v?\d", tok)
                ):
                    continue
                line = text.count("\n", 0, m.start()) + 1
                hits.append(
                    {
                        "path": rel,
                        "line": line,
                        "kind": "api",
                        "token": tok,
                        "snippet": _snippet_at(text, m.start(), m.end()),
                    }
                )
                files_with_api_or_model.add(rel)
                if len(hits) >= _MAX_HITS:
                    break
        if len(hits) >= _MAX_HITS:
            break

    gaps: list[str] = []
    for rel in sorted(import_set):
        if rel in files_with_import and rel not in files_with_api_or_model:
            gaps.append(
                f"{rel}: imports {pkg} but no model/api hits in dossier yet"
            )
    for hit in hits:
        tok = str(hit.get("token") or "")
        kind = str(hit.get("kind") or "")
        if kind == "model" and tok and tok not in already_models:
            gaps.append(f"hit model {tok!r} in {hit.get('path')} not in already_found")
        if kind == "api" and tok and tok not in already_apis:
            gaps.append(f"hit api {tok!r} in {hit.get('path')} not in already_found")
    # Cap gaps
    gaps = list(dict.fromkeys(gaps))[:40]

    dossier: dict[str, Any] = {
        "package": pkg,
        "installed_version": state.installed_version,
        "already_found": {
            "model_ids": list(state.model_ids),
            "api_patterns": list(state.api_patterns),
            "usages": [dict(u) for u in (state.usages or [])],
        },
        "path_allowlist": allowlist,
        "hits": hits[:_MAX_HITS],
        "gaps_to_check": gaps,
    }
    raw = json.dumps(dossier, ensure_ascii=False)
    while len(raw) > _MAX_DOSSIER_CHARS and dossier["hits"]:
        # Truncate snippets first, then drop hits from the end
        for h in dossier["hits"]:
            sn = str(h.get("snippet") or "")
            if len(sn) > 60:
                h["snippet"] = sn[:60] + "…"
        raw = json.dumps(dossier, ensure_ascii=False)
        if len(raw) <= _MAX_DOSSIER_CHARS:
            break
        dossier["hits"].pop()
        raw = json.dumps(dossier, ensure_ascii=False)
    if len(raw) > _MAX_DOSSIER_CHARS:
        dossier["truncated"] = True
    return dossier


def _agent_enrich(
    state: PackageClientState,
    *,
    root: Path,
    files: list[Path],
    log: Any | None = None,
) -> PackageClientState:
    """Agent/LLM pass; merge only tokens that appear in the consumer repo."""
    try:
        from conduit.llm.client import attach_llm_log, get_llm_client
    except ImportError:
        return state

    emit = log if callable(log) else None
    client = attach_llm_log(get_llm_client(), emit)
    if client is None:
        return state

    corpus_lower = _file_corpus(files).lower()
    if not corpus_lower.strip():
        return state

    known_files = {_rel(p, root) for p in files}
    try:
        dossier = build_usage_dossier(root, state.package, state, files)
    except Exception as exc:  # noqa: BLE001 — thin seed fallback
        dossier = {
            "package": state.package,
            "installed_version": state.installed_version,
            "already_found": {
                "model_ids": list(state.model_ids),
                "api_patterns": list(state.api_patterns),
                "usages": [],
            },
            "path_allowlist": sorted(state.import_files)[:40],
            "hits": [],
            "gaps_to_check": [],
            "dossier_error": str(exc),
        }

    allow = {
        str(p).replace("\\", "/")
        for p in (dossier.get("path_allowlist") or [])
        if p
    }
    prompt = {
        "package": state.package,
        "usage_dossier": dossier,
        "instructions": (
            "Extend the usage_dossier for this dependency. "
            "already_found and hits are mechanical seeds — confirm gaps and find "
            "call surfaces the cheap scan missed. "
            "Do not list the whole repo. Only read_file / grep paths in "
            "path_allowlist. "
            "Return JSON only with keys: model_ids (string[]), api_patterns (string[]), "
            "usages (list of {id, callees, paths, files}). "
            "Prefer tokens not already in already_found when you have evidence. "
            "Every id/callee/path must appear verbatim in a file you read. "
            "Do not invent model ids or APIs."
        ),
    }
    system = (
        "You map a client repository's real usage of one dependency. "
        "Start from the usage dossier. Prefer tools on allowlisted paths only. "
        "Final reply is JSON only."
    )
    data: dict[str, Any] | None = None
    try:
        from conduit.llm.executors import RepoToolExecutor
        from conduit.llm.tools import (
            agent_tools,
            resolve_max_turns,
            resolve_reasoning_effort,
        )

        run_agent = getattr(client, "run_agent", None)
        if callable(run_agent):
            max_turns = min(_ENRICH_MAX_TURNS, resolve_max_turns(32))
            if emit is not None:
                emit(
                    f"LLM client enrichment for {state.package} "
                    f"(effort={resolve_reasoning_effort()}, max_turns={max_turns}, "
                    f"dossier_hits={len(dossier.get('hits') or [])})…"
                )
            executor = RepoToolExecutor(
                root=root,
                allow_writes=False,
                allow_run_tests=False,
                path_allowlist=allow,
            )
            data = run_agent(
                system=system,
                user=json.dumps(prompt),
                tools=agent_tools(mode="enrich_scoped"),
                tool_executor=executor,
                max_turns=max_turns,
            )
        else:
            if emit is not None:
                emit(
                    f"LLM client enrichment for {state.package} "
                    f"(effort={resolve_reasoning_effort()}, one-shot)…"
                )
            data = client.complete_json(system=system, user=json.dumps(prompt))
    except Exception as exc:  # noqa: BLE001 — fail soft
        state.notes.append(f"llm enrichment failed: {exc}")
        return state

    if not isinstance(data, dict):
        state.notes.append("llm enrichment returned empty/invalid JSON")
        return state

    known_models: set[str] = set()
    known_lower: dict[str, str] = {}
    try:
        from conduit.detect.vendor_profile import collect_known_ids

        known_models = collect_known_ids(state.package, demo=False)
        known_lower = {k.lower(): k for k in known_models}
    except Exception:  # noqa: BLE001 — fail soft; corpus-only merge below
        known_models = set()
        known_lower = {}

    added_models = 0
    added_apis = 0
    added_usages = 0
    for raw in data.get("model_ids") or []:
        token = str(raw).strip()
        if not _token_in_corpus(token, corpus_lower):
            continue
        if known_models:
            canon = known_lower.get(token.lower())
            if not canon:
                continue
            token = canon
        if token not in state.model_ids:
            state.model_ids.append(token)
            added_models += 1
    for raw in data.get("api_patterns") or []:
        token = str(raw).strip()
        if not _token_in_corpus(token, corpus_lower):
            continue
        if token not in state.api_patterns:
            state.api_patterns.append(token)
            added_apis += 1

    by_id = {str(u.get("id")).lower(): u for u in state.usages if isinstance(u, dict)}
    for raw in data.get("usages") or []:
        usage = _normalize_usage(raw, corpus_lower=corpus_lower, known_files=known_files)
        if usage is None:
            continue
        key = usage["id"].lower()
        if key in by_id:
            _merge_usage_row(by_id[key], usage)
        else:
            state.usages.append(usage)
            by_id[key] = usage
            added_usages += 1
        for callee in usage.get("callees") or []:
            if callee not in state.api_patterns:
                state.api_patterns.append(callee)
                added_apis += 1
        for path in usage.get("paths") or []:
            if path not in state.api_patterns:
                state.api_patterns.append(path)
                added_apis += 1

    state.model_ids = sorted(set(state.model_ids))
    state.api_patterns = sorted(set(state.api_patterns))
    state.source = "agent"
    state.notes.append(
        f"agent scan merged model_ids=+{added_models} "
        f"api_patterns=+{added_apis} usages=+{added_usages}"
    )
    if state.model_ids:
        state.notes = [
            n for n in state.notes if not n.startswith("no model ids found")
        ]
    return state


def scan_package_state(
    root: Path,
    package: str,
    *,
    installed: dict[str, str] | None = None,
    demo: bool = False,
    use_llm: bool = True,
    log: Any | None = None,
) -> PackageClientState:
    """Scan one package's client usage (regex bootstrap; agent when LLM is on)."""
    installed = installed or {}
    state = _regex_scan_package(root, package, installed=installed, demo=demo)
    if demo:
        state.source = "demo" if state.source == "regex" else state.source
        return state
    if not use_llm:
        return state

    files = prune_by_imports(root, [package])
    config_files = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and not any(part in SKIP_DIRS for part in p.parts)
        and not _is_docish(p)
        and p.suffix.lower() in {".env", ".yaml", ".yml", ".toml", ".json", ".ini"}
    ]
    return _agent_enrich(
        state,
        root=root,
        files=list(dict.fromkeys([*files, *config_files])),
        log=log,
    )


def scan_package_states(
    root: Path,
    packages: list[str],
    *,
    installed: dict[str, str] | None = None,
    demo: bool = False,
    use_llm: bool = True,
    log: Any | None = None,
) -> dict[str, PackageClientState]:
    """Scan client state for each package name."""
    out: dict[str, PackageClientState] = {}
    for pkg in packages:
        if not pkg:
            continue
        key = pkg.lower()
        if key in out:
            continue
        out[key] = scan_package_state(
            root,
            pkg,
            installed=installed,
            demo=demo,
            use_llm=use_llm,
            log=log,
        )
    return out
