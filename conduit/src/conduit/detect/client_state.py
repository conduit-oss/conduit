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


@dataclass
class PackageClientState:
    """Where the client repo stands for one dependency package."""

    package: str
    installed_version: str | None = None
    model_ids: list[str] = field(default_factory=list)
    import_files: list[str] = field(default_factory=list)
    api_patterns: list[str] = field(default_factory=list)
    ecosystems: list[str] = field(default_factory=list)
    source: str = "regex"  # regex | regex+llm | demo
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "installed_version": self.installed_version,
            "model_ids": list(self.model_ids),
            "import_files": list(self.import_files),
            "api_patterns": list(self.api_patterns),
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
    packs = PACKAGE_PATTERN_PACKS.get(package.lower(), {})
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
    if package.lower() == "openai":
        try:
            from conduit.detect.modules.openai.known_models import (
                collect_known_model_ids,
                extract_model_kwarg_ids,
                find_known_models_in_text,
            )

            known = collect_known_model_ids(demo=demo)
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
    if package.lower() == "openai" and known:
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


def _snippet_budget(files: list[Path], root: Path, *, max_chars: int = 12000) -> str:
    parts: list[str] = []
    used = 0
    for path in files[:40]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        chunk = text if len(text) <= 2000 else text[:2000] + "\n# ... truncated ..."
        block = f"----- {_rel(path, root)} -----\n{chunk}\n"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def _llm_enrich(
    state: PackageClientState,
    *,
    root: Path,
    files: list[Path],
) -> PackageClientState:
    """Optional LLM pass; merges only tokens grounded in provided snippets."""
    try:
        from conduit.llm.client import get_llm_client
    except ImportError:
        return state

    client = get_llm_client()
    if client is None:
        return state

    snippets = _snippet_budget(files, root)
    if not snippets.strip():
        return state

    system = (
        "You analyze a client repository's usage of one dependency package. "
        "Return a single JSON object with keys model_ids (string array) and "
        "api_patterns (string array). Only include values that appear verbatim "
        "in the provided snippets. Do not invent model ids or APIs."
    )
    user = (
        f"package: {state.package}\n"
        f"regex_model_ids: {json.dumps(state.model_ids)}\n"
        f"regex_api_patterns: {json.dumps(state.api_patterns)}\n\n"
        f"snippets:\n{snippets}"
    )
    try:
        data = client.complete_json(system=system, user=user)
    except Exception as exc:  # noqa: BLE001 — fail soft
        state.notes.append(f"llm enrichment failed: {exc}")
        return state

    snippet_lower = snippets.lower()
    added_models = 0
    added_apis = 0
    for raw in data.get("model_ids") or []:
        token = str(raw).strip()
        if not token or token.lower() not in snippet_lower:
            continue
        if token not in state.model_ids:
            state.model_ids.append(token)
            added_models += 1
    for raw in data.get("api_patterns") or []:
        token = str(raw).strip()
        if not token or token.lower() not in snippet_lower:
            continue
        if token not in state.api_patterns:
            state.api_patterns.append(token)
            added_apis += 1

    state.model_ids = sorted(set(state.model_ids))
    state.api_patterns = sorted(set(state.api_patterns))
    state.source = "regex+llm"
    state.notes.append(
        f"llm enrichment merged model_ids=+{added_models} api_patterns=+{added_apis}"
    )
    # Drop the "unknown" note if we now have models
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
) -> PackageClientState:
    """Scan one package's client usage (regex always; LLM optional)."""
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
    return _llm_enrich(state, root=root, files=list(dict.fromkeys([*files, *config_files])))


def scan_package_states(
    root: Path,
    packages: list[str],
    *,
    installed: dict[str, str] | None = None,
    demo: bool = False,
    use_llm: bool = True,
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
        )
    return out
