"""Generate packet-derived oracle tests (and optional LLM extras)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from conduit.llm import get_llm_client
from conduit.repair_ignore import IgnoreList, build_ignore_list

_TOKEN_CHAR = r"A-Za-z0-9_." + r"-"

_ORACLE_PY = "tests/test_conduit_oracle.py"
_ORACLE_JS = "conduit_oracle.test.js"
_ORACLE_RELS = frozenset({_ORACLE_PY, _ORACLE_JS})

_RULE_TOKEN_KEYS = {
    "EXACT_STRING_REPLACE": "match",
    "AST_PARAM_RENAME": "old_param",
    "AST_IMPORT_REWRITE": "old_import",
    "AST_ATTR_RENAME": "old_attr",
    "AST_CALL_REWRITE": "old_callee",
    "KEY_RENAME": "old_key",
}

_MANIFEST_NAMES = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)


def token_in_text(text: str, token: str) -> bool:
    """True if ``token`` appears as a whole token (same bounds as exact_replace)."""
    if not token or token not in text:
        return False
    pattern = re.compile(
        rf"(?<![{_TOKEN_CHAR}]){re.escape(token)}(?![{_TOKEN_CHAR}])"
    )
    return pattern.search(text) is not None


def oracle_forbidden_tokens(packet: dict[str, Any]) -> list[str]:
    """Legacy strings that must not remain in scanned consumer files."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        token = token.strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)

    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rtype = str(rule.get("type") or "")
        key = _RULE_TOKEN_KEYS.get(rtype)
        if key:
            _add(str(rule.get(key) or ""))
            continue
        if rtype != "DEPENDENCY_BUMP":
            continue
        pkg = str(rule.get("package") or packet.get("package") or "").strip()
        from_v = str(rule.get("from_version") or "").strip()
        if not pkg or not from_v:
            continue
        _add(f"{pkg}=={from_v}")
        _add(f'"{pkg}": "{from_v}"')
        _add(f"'{pkg}': '{from_v}'")
    return out


def _as_rel(root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def oracle_scan_rels(
    root: Path,
    packet: dict[str, Any],
    *,
    changed_files: Iterable[str] | None = None,
    file_allowlist: Iterable[Path | str] | None = None,
    ignore: IgnoreList | None = None,
) -> list[str]:
    """Repo-relative paths the oracle should scan (ignore list applied)."""
    root = root.resolve()
    ignore = ignore or IgnoreList()
    seen: set[str] = set()
    out: list[str] = []

    def _add(path: Path) -> None:
        if not path.is_file():
            return
        rel = _as_rel(root, path)
        if not rel or rel in seen:
            return
        if rel in _ORACLE_RELS or Path(rel).name in {
            "test_conduit_oracle.py",
            "conduit_oracle.test.js",
        }:
            return
        if ignore.path_ignored(rel):
            return
        seen.add(rel)
        out.append(rel)

    for item in file_allowlist or []:
        _add(item if isinstance(item, Path) else root / str(item))
    for rel in changed_files or []:
        _add(root / str(rel))

    if any(
        isinstance(rule, dict) and str(rule.get("type") or "") == "DEPENDENCY_BUMP"
        for rule in packet.get("rules") or []
    ):
        for name in _MANIFEST_NAMES:
            _add(root / name)

    if any(
        isinstance(rule, dict) and str(rule.get("type") or "") == "KEY_RENAME"
        for rule in packet.get("rules") or []
    ):
        from conduit.patcher.key_rename import iter_config_files

        for path in iter_config_files(root):
            _add(path)

    return sorted(out)


def _is_oracle_rel(rel: str) -> bool:
    posix = rel.replace("\\", "/")
    return posix in _ORACLE_RELS or Path(posix).name in {
        "test_conduit_oracle.py",
        "conduit_oracle.test.js",
    }


def _has_consumer_tests(root: Path, *, include_oracle: bool = False) -> bool:
    """True if the repo already has tests (optionally counting Conduit oracles)."""
    hits: list[Path] = []
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        hits.extend(tests_dir.rglob("test_*.py"))
    hits.extend(root.glob("test_*.py"))
    hits.extend(root.rglob("*.test.js"))
    hits.extend(root.rglob("*.test.ts"))
    hits.extend(root.rglob("*.spec.js"))
    hits.extend(root.rglob("*.spec.ts"))
    for path in hits:
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if not include_oracle and _is_oracle_rel(rel):
            continue
        return True
    return False


def ensure_tests(
    root: Path,
    packet: dict[str, Any],
    *,
    changed_files: list[str] | None = None,
    file_allowlist: Iterable[Path | str] | None = None,
) -> list[str]:
    """
    Write/update a packet-derived leftover-token oracle.

    Always regenerates the oracle when there are tokens and files to scan.
    When the packet has no extractable tokens and the repo has no other tests,
    writes a one-line import smoke. Optional LLM extras run only when no
    consumer tests exist besides the oracle (and never overwrite the oracle).
    """
    root = root.resolve()
    ignore = build_ignore_list(root, packet)
    tokens = oracle_forbidden_tokens(packet)
    scan_rels = oracle_scan_rels(
        root,
        packet,
        changed_files=changed_files,
        file_allowlist=file_allowlist,
        ignore=ignore,
    )
    ecosystem = str(packet.get("ecosystem") or "pypi")
    package = str(packet.get("package") or "unknown")
    created: list[str] = []

    if tokens and scan_rels:
        created.append(
            _write_oracle(
                root,
                packet,
                ecosystem=ecosystem,
                files=scan_rels,
                forbidden=tokens,
            )
        )
    elif not tokens and not _has_consumer_tests(root, include_oracle=True):
        created.append(_write_import_smoke(root, package, packet, ecosystem=ecosystem))

    if not _has_consumer_tests(root, include_oracle=False):
        extra = _llm_generate_tests(
            root, packet, changed_files=changed_files or [], ecosystem=ecosystem
        )
        created.extend(extra)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for rel in created:
        if rel not in seen:
            seen.add(rel)
            unique.append(rel)
    return unique


def _write_oracle(
    root: Path,
    packet: dict[str, Any],
    *,
    ecosystem: str,
    files: list[str],
    forbidden: list[str],
) -> str:
    if ecosystem == "npm":
        return _write_js_oracle(root, packet, files=files, forbidden=forbidden)
    return _write_pytest_oracle(root, packet, files=files, forbidden=forbidden)


def _write_import_smoke(
    root: Path, package: str, packet: dict[str, Any], *, ecosystem: str
) -> str:
    from_v = packet.get("from_version", "?")
    to_v = packet.get("to_version", "?")
    if ecosystem == "npm":
        path = root / _ORACLE_JS
        content = (
            f"/** Auto-generated by Conduit — import smoke for {package} "
            f"{from_v} -> {to_v}. */\n"
            f"test('package importable', () => {{\n"
            f"  expect(require({json.dumps(package)})).toBeTruthy();\n"
            f"}});\n"
        )
        path.write_text(content, encoding="utf-8")
        return _ORACLE_JS

    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    path = tests / "test_conduit_oracle.py"
    content = f'''"""Auto-generated by Conduit — import smoke for {package} {from_v} -> {to_v}."""

from __future__ import annotations


def test_package_importable():
    """Ensure the migrated dependency still imports."""
    mod = __import__({package!r})
    assert mod is not None
'''
    path.write_text(content, encoding="utf-8")
    return _ORACLE_PY


def _write_pytest_oracle(
    root: Path,
    packet: dict[str, Any],
    *,
    files: list[str],
    forbidden: list[str],
) -> str:
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    path = tests / "test_conduit_oracle.py"
    package = packet.get("package") or "unknown"
    from_v = packet.get("from_version", "?")
    to_v = packet.get("to_version", "?")
    files_lit = json.dumps(files, indent=4)
    forbidden_lit = json.dumps(forbidden, indent=4)
    content = f'''"""Auto-generated by Conduit — leftover-token oracle for {package} {from_v} -> {to_v}.

Do not edit; regenerated on each `conduit run`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {files_lit}
FORBIDDEN = {forbidden_lit}
_TOKEN_CHAR = r"A-Za-z0-9_." + r"-"


def _has_token(text: str, token: str) -> bool:
    if not token or token not in text:
        return False
    pattern = re.compile(
        rf"(?<![{{_TOKEN_CHAR}}]){{re.escape(token)}}(?![{{_TOKEN_CHAR}}])"
    )
    return pattern.search(text) is not None


def test_conduit_no_legacy_tokens():
    failures = []
    for rel in FILES:
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if _has_token(text, token):
                failures.append(f"{{rel}} still contains {{token!r}}")
    assert not failures, "\\n".join(failures)
'''
    path.write_text(content, encoding="utf-8")
    return _ORACLE_PY


def _write_js_oracle(
    root: Path,
    packet: dict[str, Any],
    *,
    files: list[str],
    forbidden: list[str],
) -> str:
    package = packet.get("package") or "unknown"
    from_v = packet.get("from_version", "?")
    to_v = packet.get("to_version", "?")
    content = """/** Auto-generated by Conduit — leftover-token oracle for %(package)s %(from_v)s -> %(to_v)s.
 * Do not edit; regenerated on each `conduit run`.
 */
const fs = require('fs');
const path = require('path');

const FILES = %(files_lit)s;
const FORBIDDEN = %(forbidden_lit)s;

function hasToken(text, token) {
  if (!token || !text.includes(token)) return false;
  const escaped = token.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
  const re = new RegExp('(?<![A-Za-z0-9_.-])' + escaped + '(?![A-Za-z0-9_.-])');
  return re.test(text);
}

test('conduit no legacy tokens', () => {
  const failures = [];
  for (const rel of FILES) {
    const full = path.join(process.cwd(), rel);
    if (!fs.existsSync(full)) continue;
    const text = fs.readFileSync(full, 'utf8');
    for (const token of FORBIDDEN) {
      if (hasToken(text, token)) {
        failures.push(rel + ' still contains ' + JSON.stringify(token));
      }
    }
  }
  expect(failures).toEqual([]);
});
""" % {
        "package": package,
        "from_v": from_v,
        "to_v": to_v,
        "files_lit": json.dumps(files, indent=2),
        "forbidden_lit": json.dumps(forbidden, indent=2),
    }
    path = root / _ORACLE_JS
    path.write_text(content, encoding="utf-8")
    return _ORACLE_JS


def _llm_generate_tests(
    root: Path,
    packet: dict[str, Any],
    *,
    changed_files: list[str],
    ecosystem: str,
) -> list[str]:
    client = get_llm_client()
    if client is None:
        return []

    samples: dict[str, str] = {}
    for rel in changed_files[:8]:
        path = root / rel
        if path.is_file():
            try:
                samples[rel] = path.read_text(encoding="utf-8")[:4000]
            except OSError:
                continue

    prompt = {
        "instructions": (
            "Generate a minimal extra smoke test for a repo after an API migration. "
            'Return JSON: {"files": {"relative/path": "full file contents"}}. '
            "For Python use tests/test_conduit_smoke.py with pytest. "
            "For npm use conduit_smoke.test.js. Do not require network calls. "
            "Do not write tests/test_conduit_oracle.py or conduit_oracle.test.js."
        ),
        "ecosystem": ecosystem,
        "packet": {
            "package": packet.get("package"),
            "from_version": packet.get("from_version"),
            "to_version": packet.get("to_version"),
            "rules": packet.get("rules") or [],
        },
        "changed_files": samples,
    }
    try:
        data = client.complete_json(
            system="You write minimal migration smoke tests. JSON only.",
            user=json.dumps(prompt),
        )
    except Exception:
        return []

    files = data.get("files") or {}
    created: list[str] = []
    root_resolved = root.resolve()
    for rel, content in files.items():
        if not isinstance(content, str):
            continue
        rel_posix = str(rel).replace("\\", "/")
        if _is_oracle_rel(rel_posix):
            continue
        path = (root / str(rel)).resolve()
        if not str(path).startswith(str(root_resolved)):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(rel_posix)
    return created
