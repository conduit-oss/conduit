"""Generate packet-derived oracle tests (and optional LLM extras)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from conduit.llm import get_llm_client
from conduit.repair_ignore import IgnoreList, build_ignore_list
from conduit.text_tokens import token_in_text as _token_in_text

_ORACLE_PY = "tests/test_conduit_oracle.py"
_ORACLE_JS = "conduit_oracle.test.js"
_SMOKE_PY = "tests/test_conduit_smoke.py"
_SMOKE_JS = "conduit_smoke.test.js"
_FUNCTIONAL_PY = "tests/test_conduit_functional.py"
_FUNCTIONAL_JS = "conduit_functional.test.js"
CONDUIT_GENERATED_NAMES = frozenset(
    {
        "test_conduit_oracle.py",
        "conduit_oracle.test.js",
        "test_conduit_smoke.py",
        "conduit_smoke.test.js",
        "test_conduit_functional.py",
        "conduit_functional.test.js",
    }
)
_ORACLE_RELS = frozenset(
    {_ORACLE_PY, _ORACLE_JS, _SMOKE_PY, _SMOKE_JS, _FUNCTIONAL_PY, _FUNCTIONAL_JS}
)
# Public alias for ignore-list / repair callers.
CONDUIT_GENERATED_RELS = _ORACLE_RELS


def is_conduit_generated_rel(rel: str) -> bool:
    posix = rel.replace("\\", "/")
    name = Path(posix).name
    if posix in _ORACLE_RELS or name in CONDUIT_GENERATED_NAMES:
        return True
    if name.startswith("test_conduit_") and name.endswith(".py"):
        return True
    if name.startswith("conduit_") and name.endswith((".test.js", ".test.ts")):
        return True
    return False

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
    return _token_in_text(text, token)


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
    extra: list[str] = []
    for token in list(out):
        if token.startswith("/v1/") and len(token) > 4:
            extra.append(token[3:])  # "/fine-tunes" from "/v1/fine-tunes"
    for token in extra:
        _add(token)
    return out


def _as_rel(root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _is_prose_ops_surface(rel: str) -> bool:
    from conduit.surface_paths import is_prose_ops_rel

    return is_prose_ops_rel(rel)


def oracle_scan_rels(
    root: Path,
    packet: dict[str, Any],
    *,
    changed_files: Iterable[str] | None = None,
    file_allowlist: Iterable[Path | str] | None = None,
    ignore: IgnoreList | None = None,
) -> list[str]:
    """Repo-relative paths the leftover oracle should scan (ignore + prose excluded)."""
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
        if is_conduit_generated_rel(rel):
            return
        if ignore.path_ignored(rel):
            return
        # Docs/README/scripts/ops/Dockerfile sync after green — not leftover-fail.
        if _is_prose_ops_surface(rel):
            return
        seen.add(rel)
        out.append(rel)

    for item in file_allowlist or []:
        _add(item if isinstance(item, Path) else root / str(item))
    for rel in changed_files or []:
        _add(root / str(rel))

    for name in _MANIFEST_NAMES:
        _add(root / name)

    from conduit.patcher.key_rename import iter_config_files as _iter_cfg

    # Always scan configs even when import-prune dropped them.
    for path in _iter_cfg(root):
        _add(path)
    # configs/ and .github stay in leftover oracle; scripts/ops is prose/ops.
    for extra_dir in ("configs", ".github"):
        folder = root / extra_dir
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix.lower() in {
                ".py",
                ".ts",
                ".js",
                ".sh",
                ".yml",
                ".yaml",
                ".json",
                ".toml",
            }:
                _add(path)
    # Neighbor source files in the same directory as allowlisted impl.
    neighbors: list[Path] = []
    for rel in list(out):
        parent = (root / rel).parent
        if not parent.is_dir():
            continue
        for sib in parent.iterdir():
            if sib.is_file() and sib.suffix.lower() in {
                ".py",
                ".ts",
                ".js",
                ".tsx",
                ".jsx",
            }:
                neighbors.append(sib)
    for path in neighbors:
        _add(path)

    return sorted(out)


def _is_oracle_rel(rel: str) -> bool:
    return is_conduit_generated_rel(rel)


def _is_impl_rel(rel: str) -> bool:
    posix = rel.replace("\\", "/")
    name = Path(posix).name
    if posix.startswith("tests/") or "/tests/" in posix:
        return False
    if name.startswith("test_") or name.endswith(".test.js") or name.endswith(".spec.js"):
        return False
    if name in _MANIFEST_NAMES or name == "constraints.txt":
        return False
    return True


def oracle_required_appearances(
    packet: dict[str, Any],
    source: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """New callees/params that must appear in impl files when the client used the old name."""
    from conduit.packet.scope import _token_in_index
    from conduit.packet.synthesize import _source_usage_index

    index = _source_usage_index(source)
    if not index["has_usage"]:
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rtype = str(rule.get("type") or "")
        pairs: list[tuple[str, str, str]] = []
        if rtype == "AST_CALL_REWRITE":
            pairs.append(
                (
                    str(rule.get("old_callee") or ""),
                    str(rule.get("new_callee") or ""),
                    "callee",
                )
            )
        elif rtype == "AST_PARAM_RENAME":
            pairs.append(
                (
                    str(rule.get("old_param") or ""),
                    str(rule.get("new_param") or ""),
                    "param",
                )
            )
        elif rtype == "AST_ATTR_RENAME":
            pairs.append(
                (
                    str(rule.get("old_attr") or ""),
                    str(rule.get("new_attr") or ""),
                    "attr",
                )
            )
        elif rtype == "AST_IMPORT_REWRITE":
            pairs.append(
                (
                    str(rule.get("old_import") or ""),
                    str(rule.get("new_import") or ""),
                    "import",
                )
            )
        elif rtype == "EXACT_STRING_REPLACE":
            pairs.append(
                (
                    str(rule.get("match") or ""),
                    str(rule.get("replace") or ""),
                    "replace",
                )
            )
        for old, new, kind in pairs:
            old, new = old.strip(), new.strip()
            if not old or not new or old.lower() == new.lower():
                continue
            if not _token_in_index(old, index):
                continue
            key = (kind, new)
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": kind, "old": old, "new": new})
    return out


def _packet_has_ast_rules(packet: dict[str, Any]) -> bool:
    return any(
        isinstance(r, dict)
        and str(r.get("type") or "").startswith("AST_")
        for r in (packet.get("rules") or [])
    )


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
    source: dict[str, Any] | None = None,
) -> list[str]:
    """
    Write/update leftover-token oracle, migration smoke, and (when an LLM is
    configured) functional tests that exercise new endpoints.

    Always regenerates the leftover oracle when there are tokens and files to
    scan. Shape/smoke tests are written whenever the packet has in-scope AST
    or string rules. LLM functional tests run in addition to leftover/smoke and
    never overwrite the leftover oracle.
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
    required = oracle_required_appearances(packet, source)
    impl_rels = [rel for rel in scan_rels if _is_impl_rel(rel)]
    created: list[str] = []

    if tokens and scan_rels:
        created.append(
            _write_oracle(
                root,
                packet,
                ecosystem=ecosystem,
                files=scan_rels,
                forbidden=tokens,
                required=required,
                impl_files=impl_rels,
            )
        )
    elif not tokens and not _has_consumer_tests(root, include_oracle=True):
        created.append(_write_import_smoke(root, package, packet, ecosystem=ecosystem))

    smoke = _write_smoke(
        root,
        packet,
        ecosystem=ecosystem,
        impl_files=impl_rels,
        required=required,
        changed_files=changed_files or [],
    )
    if smoke:
        created.append(smoke)

    extra = _llm_generate_functional_tests(
        root,
        packet,
        changed_files=changed_files or [],
        ecosystem=ecosystem,
        source=source,
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
    required: list[dict[str, str]] | None = None,
    impl_files: list[str] | None = None,
) -> str:
    if ecosystem == "npm":
        return _write_js_oracle(
            root,
            packet,
            files=files,
            forbidden=forbidden,
            required=required or [],
            impl_files=impl_files or [],
        )
    return _write_pytest_oracle(
        root,
        packet,
        files=files,
        forbidden=forbidden,
        required=required or [],
        impl_files=impl_files or [],
    )


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
    required: list[dict[str, str]],
    impl_files: list[str],
) -> str:
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    path = tests / "test_conduit_oracle.py"
    package = packet.get("package") or "unknown"
    from_v = packet.get("from_version", "?")
    to_v = packet.get("to_version", "?")
    files_lit = json.dumps(files, indent=4)
    forbidden_lit = json.dumps(forbidden, indent=4)
    required_lit = json.dumps(required, indent=4)
    impl_lit = json.dumps(impl_files, indent=4)
    content = f'''"""Auto-generated by Conduit — leftover-token oracle for {package} {from_v} -> {to_v}.

Do not edit; regenerated on each `conduit run`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {files_lit}
IMPL_FILES = {impl_lit}
FORBIDDEN = {forbidden_lit}
REQUIRED = {required_lit}
_TOKEN_CHAR = r"A-Za-z0-9_." + r"-"
_STR_LIT = re.compile(r"""(['"])(?:\\\\.|(?!\\1).)*\\1""")
_PY_JOIN = re.compile(
    r"(?P<sep>(['\\"])(?:\\\\.|(?!\\2).)*\\2)\\s*\\.\\s*join\\s*\\(\\s*\\[\\s*"
    r"(?P<parts>(?:(['\\"])(?:\\\\.|(?!\\4).)*\\4\\s*,\\s*)*(['\\"])(?:\\\\.|(?!\\5).)*\\5)"
    r"\\s*\\]\\s*\\)",
    re.DOTALL,
)
_JS_JOIN = re.compile(
    r"\\[\\s*(?P<parts>(?:(['\\"])(?:\\\\.|(?!\\2).)*\\2\\s*,\\s*)*(['\\"])(?:\\\\.|(?!\\3).)*\\3)"
    r"\\s*\\]\\s*\\.\\s*join\\s*\\(\\s*(?P<sep>(['\\"])(?:\\\\.|(?!\\5).)*\\5)\\s*\\)",
    re.DOTALL,
)


def _has_token(text: str, token: str) -> bool:
    if not token or token not in text:
        return False
    pattern = re.compile(
        rf"(?<![{{_TOKEN_CHAR}}]){{re.escape(token)}}(?![{{_TOKEN_CHAR}}])"
    )
    return pattern.search(text) is not None


def _dotted(node):
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return ".".join(parts)


def _callee_used(text, callee):
    callee = callee or ""
    if not callee:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return callee + "(" in text
    found = set()

    class _C(ast.NodeVisitor):
        def visit_Call(self, node):
            dotted = _dotted(node.func)
            if dotted:
                found.add(dotted)
            self.generic_visit(node)

        def visit_Attribute(self, node):
            dotted = _dotted(node)
            if dotted:
                found.add(dotted)
            self.generic_visit(node)

    _C().visit(tree)
    if callee in found:
        return True
    return any(item.endswith("." + callee) or item.endswith(callee) for item in found)


def _active_token(text, token):
    if not _has_token(text, token):
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return True
    marker_re = re.compile(r"(MARKER|MIGRATION)", re.I)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not node.targets or not isinstance(node.targets[0], ast.Name):
            continue
        if not marker_re.search(node.targets[0].id):
            continue
        seq = node.value
        elts = getattr(seq, "elts", None)
        if elts is None:
            continue
        if any(isinstance(elt, ast.Constant) and elt.value == token for elt in elts):
            # token only lives in an unused marker tuple
            name = node.targets[0].id
            class _L(ast.NodeVisitor):
                loads = 0
                def visit_Name(self, n):
                    if n.id == name and isinstance(n.ctx, ast.Load):
                        self.loads += 1
            v = _L()
            v.visit(tree)
            if v.loads == 0:
                return False
    return True


def _unquote(lit: str) -> str:
    try:
        return ast.literal_eval(lit)
    except (ValueError, SyntaxError):
        if len(lit) >= 2 and lit[0] == lit[-1] and lit[0] in {{"'", '"'}}:
            return lit[1:-1]
        return lit


def _join_from_parts(parts_blob: str, sep_lit: str) -> str | None:
    parts = [_unquote(m.group(0)) for m in _STR_LIT.finditer(parts_blob)]
    if not parts:
        return None
    return _unquote(sep_lit).join(parts)


def _reconstructed_literals(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def _add(value: str | None) -> None:
        if value and value not in seen:
            seen.add(value)
            found.append(value)

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        class _V(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "join":
                    sep = func.value.value if isinstance(func.value, ast.Constant) else None
                    if isinstance(sep, str) and node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
                        parts = [
                            elt.value
                            for elt in node.args[0].elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        ]
                        if len(parts) == len(node.args[0].elts):
                            _add(sep.join(parts))
                self.generic_visit(node)

            def visit_BinOp(self, node: ast.BinOp) -> None:
                if isinstance(node.op, ast.Add):
                    def fold(n):
                        if isinstance(n, ast.Constant) and isinstance(n.value, str):
                            return n.value
                        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                            left, right = fold(n.left), fold(n.right)
                            if left is not None and right is not None:
                                return left + right
                        return None
                    _add(fold(node))
                self.generic_visit(node)

        _V().visit(tree)
    for match in _PY_JOIN.finditer(text):
        _add(_join_from_parts(match.group("parts"), match.group("sep")))
    for match in _JS_JOIN.finditer(text):
        _add(_join_from_parts(match.group("parts"), match.group("sep")))
    return found


def test_conduit_no_legacy_tokens():
    failures = []
    for rel in FILES:
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        rebuilt = _reconstructed_literals(text)
        for token in FORBIDDEN:
            if _has_token(text, token):
                failures.append(f"{{rel}} still contains {{token!r}}")
            elif any(token == item or _has_token(item, token) for item in rebuilt):
                failures.append(f"{{rel}} obfuscates leftover {{token!r}} via concat/join")
    assert not failures, "\\n".join(failures)


def test_conduit_required_shapes_present():
    if not REQUIRED:
        return
    failures = []
    blobs = []
    for rel in IMPL_FILES:
        path = ROOT / rel
        if path.is_file():
            blobs.append(path.read_text(encoding="utf-8"))
    combined = "\\n".join(blobs)
    for item in REQUIRED:
        new = item.get("new") or ""
        kind = item.get("kind") or ""
        if not new:
            continue
        if kind in {{"callee", "attr", "param"}}:
            ok = False
            for blob in blobs:
                if kind == "param":
                    quoted = '"' + new + '"'
                    if re.search(rf"\\b{{re.escape(new)}}\\s*=", blob) or quoted in blob:
                        prefix = blob.split(new, 1)[0][-80:]
                        if "MIGRATION_MARKER" not in prefix:
                            ok = True
                            break
                elif _callee_used(blob, new):
                    ok = True
                    break
            if not ok:
                failures.append(
                    f"no impl file uses {{new!r}} as a {{kind}} (required after {{item.get('old')!r}})"
                )
        elif not any(_active_token(blob, new) for blob in blobs):
            failures.append(
                f"no impl file contains {{new!r}} (required after {{item.get('old')!r}})"
            )
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
    required: list[dict[str, str]],
    impl_files: list[str],
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
const IMPL_FILES = %(impl_lit)s;
const FORBIDDEN = %(forbidden_lit)s;
const REQUIRED = %(required_lit)s;

function hasToken(text, token) {
  if (!token || !text.includes(token)) return false;
  const escaped = token.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
  const re = new RegExp('(?<![A-Za-z0-9_.-])' + escaped + '(?![A-Za-z0-9_.-])');
  return re.test(text);
}

function reconstructedLiterals(text) {
  const out = [];
  const re = /\\[\\s*((?:(['"])(?:\\\\.|(?!\\2).)*\\2\\s*,\\s*)*(['"])(?:\\\\.|(?!\\3).)*\\3)\\s*\\]\\s*\\.\\s*join\\s*\\(\\s*(['"])((?:\\\\.|(?!\\4).)*)\\4\\s*\\)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const parts = [];
    const partRe = /(['"])((?:\\\\.|(?!\\1).)*)\\1/g;
    let p;
    while ((p = partRe.exec(m[1])) !== null) {
      parts.push(p[2]);
    }
    out.push(parts.join(m[5]));
  }
  return out;
}

test('conduit no legacy tokens', () => {
  const failures = [];
  for (const rel of FILES) {
    const full = path.join(process.cwd(), rel);
    if (!fs.existsSync(full)) continue;
    const text = fs.readFileSync(full, 'utf8');
    const rebuilt = reconstructedLiterals(text);
    for (const token of FORBIDDEN) {
      if (hasToken(text, token)) {
        failures.push(rel + ' still contains ' + JSON.stringify(token));
      } else if (rebuilt.some((item) => item === token || hasToken(item, token))) {
        failures.push(rel + ' obfuscates leftover ' + JSON.stringify(token));
      }
    }
  }
  expect(failures).toEqual([]);
});

test('conduit required shapes present', () => {
  if (!REQUIRED.length) return;
  const blobs = [];
  for (const rel of IMPL_FILES) {
    const full = path.join(process.cwd(), rel);
    if (fs.existsSync(full)) blobs.push(fs.readFileSync(full, 'utf8'));
  }
  const combined = blobs.join('\\n');
  const failures = [];
  for (const item of REQUIRED) {
    if (item.new && !hasToken(combined, item.new)) {
      failures.push('missing ' + JSON.stringify(item.new));
    }
  }
  expect(failures).toEqual([]);
});
""" % {
        "package": package,
        "from_v": from_v,
        "to_v": to_v,
        "files_lit": json.dumps(files, indent=2),
        "impl_lit": json.dumps(impl_files, indent=2),
        "forbidden_lit": json.dumps(forbidden, indent=2),
        "required_lit": json.dumps(required, indent=2),
    }
    path = root / _ORACLE_JS
    path.write_text(content, encoding="utf-8")
    return _ORACLE_JS


def _write_smoke(
    root: Path,
    packet: dict[str, Any],
    *,
    ecosystem: str,
    impl_files: list[str],
    required: list[dict[str, str]],
    changed_files: list[str],
) -> str | None:
    """Deterministic migration smoke: required new tokens + import changed modules."""
    if not required and not impl_files and not changed_files:
        return None
    package = packet.get("package") or "unknown"
    from_v = packet.get("from_version", "?")
    to_v = packet.get("to_version", "?")
    py_changed = [
        rel.replace("\\", "/")
        for rel in changed_files
        if rel.replace("\\", "/").endswith(".py") and _is_impl_rel(rel)
    ]
    if ecosystem == "npm":
        path = root / _SMOKE_JS
        content = """/** Auto-generated by Conduit — migration smoke for %(package)s %(from_v)s -> %(to_v)s. */
const fs = require('fs');
const path = require('path');
const IMPL_FILES = %(impl_lit)s;
const REQUIRED = %(required_lit)s;

function hasToken(text, token) {
  if (!token || !text.includes(token)) return false;
  const escaped = token.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
  return new RegExp('(?<![A-Za-z0-9_.-])' + escaped + '(?![A-Za-z0-9_.-])').test(text);
}

test('conduit smoke required shapes', () => {
  if (!REQUIRED.length) return;
  const blobs = IMPL_FILES.filter((rel) => fs.existsSync(path.join(process.cwd(), rel)))
    .map((rel) => fs.readFileSync(path.join(process.cwd(), rel), 'utf8'));
  const combined = blobs.join('\\n');
  const failures = REQUIRED.filter((item) => item.new && !hasToken(combined, item.new))
    .map((item) => item.new);
  expect(failures).toEqual([]);
});
""" % {
            "package": package,
            "from_v": from_v,
            "to_v": to_v,
            "impl_lit": json.dumps(impl_files, indent=2),
            "required_lit": json.dumps(required, indent=2),
        }
        path.write_text(content, encoding="utf-8")
        return _SMOKE_JS

    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    path = tests / "test_conduit_smoke.py"
    content = f'''"""Auto-generated by Conduit — migration smoke for {package} {from_v} -> {to_v}."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPL_FILES = {json.dumps(impl_files, indent=4)}
REQUIRED = {json.dumps(required, indent=4)}
CHANGED_PY = {json.dumps(py_changed, indent=4)}
_TOKEN_CHAR = r"A-Za-z0-9_." + r"-"


def _has_token(text: str, token: str) -> bool:
    if not token or token not in text:
        return False
    return re.search(
        rf"(?<![{{_TOKEN_CHAR}}]){{re.escape(token)}}(?![{{_TOKEN_CHAR}}])",
        text,
    ) is not None


def _dotted(node):
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return ".".join(parts)


def _callee_used(text, callee):
    callee = callee or ""
    if not callee:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return (callee + "(") in text
    found = set()

    class _C(ast.NodeVisitor):
        def visit_Call(self, node):
            dotted = _dotted(node.func)
            if dotted:
                found.add(dotted)
            self.generic_visit(node)

        def visit_Attribute(self, node):
            dotted = _dotted(node)
            if dotted:
                found.add(dotted)
            self.generic_visit(node)

    _C().visit(tree)
    if callee in found:
        return True
    return any(item.endswith("." + callee) or item.endswith(callee) for item in found)


def test_conduit_smoke_required_shapes():
    if not REQUIRED:
        return
    blobs = []
    for rel in IMPL_FILES:
        path = ROOT / rel
        if path.is_file():
            blobs.append(path.read_text(encoding="utf-8"))
    failures = []
    for item in REQUIRED:
        new = item.get("new") or ""
        kind = item.get("kind") or ""
        if not new:
            continue
        ok = False
        for blob in blobs:
            if kind in {{"callee", "attr"}}:
                ok = _callee_used(blob, new)
            else:
                ok = _has_token(blob, new) and "MIGRATION_MARKERS" not in blob
            if ok:
                break
        if not ok:
            failures.append(new)
    assert not failures, "missing migrated tokens: " + ", ".join(failures)


def test_conduit_smoke_changed_modules_importable():
    import importlib.util

    for rel in CHANGED_PY:
        path = ROOT / rel
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module is not None
'''
    path.write_text(content, encoding="utf-8")
    return _SMOKE_PY


def _llm_generate_functional_tests(
    root: Path,
    packet: dict[str, Any],
    *,
    changed_files: list[str],
    ecosystem: str,
    source: dict[str, Any] | None = None,
) -> list[str]:
    """LLM functional tests for new endpoints + public APIs. Never skip."""
    from conduit.integrity import audit_consumer_tests
    from conduit.self_correct import reject_self_correct_write

    client = get_llm_client()
    if client is None:
        return []

    samples: dict[str, str] = {}
    for rel in changed_files[:12]:
        path = root / rel
        if path.is_file() and not is_conduit_generated_rel(rel):
            try:
                samples[rel] = path.read_text(encoding="utf-8")[:5000]
            except OSError:
                continue

    required = oracle_required_appearances(packet, source)
    audit_notes = audit_consumer_tests(root)
    target = _FUNCTIONAL_JS if ecosystem == "npm" else _FUNCTIONAL_PY
    prompt = {
        "instructions": (
            "Write STRONG functional tests for a repo after an API migration. "
            "Exercise migrated public functions and new endpoints/callees from the "
            "packet (chat completions, models list, fine_tuning.jobs, tools vs "
            "functions, etc.). Prefer live API calls with tiny max_tokens when "
            "OPENAI_API_KEY is already in the environment. "
            "If the key is missing the test MUST fail with an assertion — never skip, "
            "xfail, or catch Exception to return a dummy. "
            "Do not trust existing consumer tests listed in audit_notes. "
            "Do not overwrite leftover oracle or smoke files. "
            "Do not use pytest.mark.skip, xfail, string join/concat, or unused "
            "MIGRATION_MARKERS tuples. "
            f'Return JSON: {{"files": {{"{target}": "full file contents"}}}}.'
        ),
        "ecosystem": ecosystem,
        "target_path": target,
        "audit_notes": audit_notes,
        "required_shapes": required,
        "packet": {
            "package": packet.get("package"),
            "from_version": packet.get("from_version"),
            "to_version": packet.get("to_version"),
            "rules": [
                {
                    "type": r.get("type"),
                    "old_callee": r.get("old_callee"),
                    "new_callee": r.get("new_callee"),
                    "old_param": r.get("old_param"),
                    "new_param": r.get("new_param"),
                    "match": r.get("match"),
                    "replace": r.get("replace"),
                }
                for r in (packet.get("rules") or [])
                if isinstance(r, dict)
            ][:80],
        },
        "changed_files": samples,
    }
    try:
        data = client.complete_json(
            system=(
                "You write real functional tests that fail loudly. JSON only. "
                "Never skip, never dummy-except, never hide tokens."
            ),
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
        if rel_posix in {_ORACLE_PY, _ORACLE_JS, _SMOKE_PY, _SMOKE_JS}:
            continue
        if Path(rel_posix).name in {
            "test_conduit_oracle.py",
            "conduit_oracle.test.js",
            "test_conduit_smoke.py",
            "conduit_smoke.test.js",
        }:
            continue
        if ecosystem == "npm" and rel_posix != _FUNCTIONAL_JS:
            if not rel_posix.endswith(".test.js"):
                continue
        if ecosystem != "npm" and rel_posix != _FUNCTIONAL_PY:
            if not rel_posix.startswith("tests/") or not rel_posix.endswith(".py"):
                continue
            if not Path(rel_posix).name.startswith("test_conduit_"):
                continue
        reason = reject_self_correct_write(rel_posix, content, packet=packet)
        if reason:
            continue
        path = (root / str(rel)).resolve()
        if not str(path).startswith(str(root_resolved)):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(rel_posix)
    return created
