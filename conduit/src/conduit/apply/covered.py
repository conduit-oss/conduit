"""Which changed lines a test run executed.

A passing test run proves a rewrite only if some test ran it. While the test
command runs, a small ``sitecustomize`` (put first on ``PYTHONPATH``, so every
Python process the command starts loads it) records the lines executed in the
rewritten files only; every other file is left untraced. Each process writes
its lines when it exits.

A rewrite counts as covered when every statement it changed ran. A statement
spanning several lines ran when any of its lines did. Lines at module or class
level run when the module is imported, so for an import, running is the proof
(a missing name fails right there). An assignment there proves nothing by
running: it counts only when code in a function that reads the name it binds
ran (when no function in the file reads it, the import is all there is).
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

_RECORDER = r'''
import json as _json, os as _os, sys as _sys, threading as _threading, atexit as _atexit

def _conduit_recorder():
    watch = _json.loads(_os.environ.get("CONDUIT_LINES_WATCH") or "{}")
    out = _os.environ.get("CONDUIT_LINES_OUT") or ""
    if not watch or not out:
        return
    hits = {}
    known = {}

    def rel_of(filename):
        rel = known.get(filename, 0)
        if rel == 0:
            try:
                rel = watch.get(_os.path.realpath(filename))
            except Exception:
                rel = None
            known[filename] = rel
        return rel

    def tracer(frame, event, arg):
        rel = rel_of(frame.f_code.co_filename)
        if rel is None:
            return None
        lines = hits.setdefault(rel, set())

        def line(frame, event, arg):
            if event == "line":
                lines.add(frame.f_lineno)
            return line

        return line

    def save():
        try:
            data = {"hits": {rel: sorted(lines) for rel, lines in hits.items()}, "replaced": _sys.gettrace() is not tracer}
            path = _os.path.join(out, "%d.json" % _os.getpid())
            with open(path, "w", encoding="utf-8") as fh:
                _json.dump(data, fh)
        except Exception:
            pass

    _sys.settrace(tracer)
    _threading.settrace(tracer)
    _atexit.register(save)


_conduit_recorder()
del _conduit_recorder


def _conduit_chain():
    """Load the sitecustomize this one shadows, if there is one."""
    import importlib.machinery as _machinery
    import importlib.util as _util

    here = _os.path.dirname(_os.path.abspath(__file__))
    paths = [p for p in _sys.path if _os.path.abspath(p or ".") != here]
    spec = _machinery.PathFinder.find_spec("sitecustomize", paths)
    if spec is None or spec.loader is None or spec.origin == __file__:
        return
    module = _util.module_from_spec(spec)
    spec.loader.exec_module(module)


try:
    _conduit_chain()
except Exception:
    pass
del _conduit_chain
'''


@dataclass
class LineRecord:
    """Lines one test run executed in the watched files, and those files as run."""

    hits: dict[str, set[int]] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)
    #: Another tracer (a line-measuring plugin, a debugger) replaced the recorder in
    #: some process, so lines it ran may be missing.
    replaced: bool = False
    #: The run reported at least one test that ran.
    tests_ran: bool = False
    #: A process wrote its line record. False when the run stopped before that.
    wrote: bool = False


@contextmanager
def recording(cwd: Path, rels: list[str], env: dict[str, str]) -> Iterator[LineRecord]:
    """Set ``env`` up to record lines of ``rels`` (under ``cwd``) while the block
    runs the command; the record is filled when the block ends."""
    record = LineRecord(texts={rel: _read(cwd / rel) for rel in rels})
    with tempfile.TemporaryDirectory(prefix="conduit-lines-") as tmp:
        home, out = Path(tmp) / "site", Path(tmp) / "out"
        home.mkdir()
        out.mkdir()
        (home / "sitecustomize.py").write_text(_RECORDER, encoding="utf-8")
        watch = {os.path.realpath(cwd / rel): rel for rel in rels}
        env["CONDUIT_LINES_WATCH"] = json.dumps(watch)
        env["CONDUIT_LINES_OUT"] = str(out)
        env["PYTHONPATH"] = os.pathsep.join(part for part in (str(home), env.get("PYTHONPATH", "")) if part)
        try:
            yield record
        finally:
            found = False
            for item in out.glob("*.json"):
                found = True
                try:
                    data = json.loads(item.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                record.replaced = record.replaced or bool(data.get("replaced"))
                for rel, lines in (data.get("hits") or {}).items():
                    record.hits.setdefault(rel, set()).update(int(n) for n in lines)
            record.wrote = found


_RAN = re.compile(r"\b([1-9]\d*) passed\b|\bRan ([1-9]\d*) tests?\b")


def tests_ran(outcomes: dict[str, str], output: str) -> bool:
    """At least one test ran and passed (per-test outcomes, else the summary line)."""
    if any(outcome == "passed" for outcome in outcomes.values()):
        return True
    return bool(_RAN.search(output or ""))


def changed_lines(original: str, current: str, first: int, last: int) -> list[int]:
    """Lines ``first..last`` of ``current`` that differ from ``original``. A pure
    deletion counts as the line now standing where the removed lines were."""
    old, new = original.splitlines(), current.splitlines()
    out: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
        elif tag == "delete":
            out.add(j1 + 1)
    return sorted(n for n in out if first <= n <= last)


@dataclass
class Reach:
    covered: bool
    #: Changed statements no test ran, as ``(first line, last line)``.
    missed: list[tuple[int, int]]


def block_reach(record: LineRecord | None, path: str, original: str, block: str) -> Reach:
    """Whether the test run executed every statement ``block`` changed in ``path``."""
    if record is None or not record.tests_ran:
        return Reach(False, [])
    text = record.texts.get(path, "")
    at = text.find(block) if block else -1
    if at < 0:
        return Reach(False, [])
    first = text.count("\n", 0, at) + 1
    last = first + max(block.count("\n") - (1 if block.endswith("\n") else 0), 0)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return Reach(False, [])
    spans = _statement_spans(tree)
    lines = text.splitlines()
    wanted = [n for n in changed_lines(original, text, first, last) if _is_code(lines, n)]
    if not wanted:
        wanted = [n for n in range(first, last + 1) if _is_code(lines, n)]
    stmts: list[ast.stmt] = []
    for number in wanted:
        stmt = _innermost(spans, number)
        if stmt is not None and stmt not in stmts:
            stmts.append(stmt)
    if not stmts:
        return Reach(False, [])
    hits = record.hits.get(path, set())
    missed = [_own(stmt) for stmt in stmts if not _proven(stmt, spans, tree, hits)]
    return Reach(not missed, missed)


def describe(path: str, missed: list[tuple[int, int]]) -> str:
    """``path:12, 14-16`` for the statements no test ran."""
    parts = [f"{a}" if a == b else f"{a}-{b}" for a, b in missed[:6]]
    return f"{path}:{', '.join(parts)}" if parts else path


# --- statements ----------------------------------------------------------------------


@dataclass
class _Spans:
    items: list[tuple[int, int, int, ast.stmt]]  # (start, end, depth, stmt)
    at_import: set[int]  # id() of statements that run on import (module/class level)


def _statement_spans(tree: ast.Module) -> _Spans:
    items: list[tuple[int, int, int, ast.stmt]] = []
    at_import: set[int] = set()

    def walk(body: list[ast.stmt], depth: int, importing: bool) -> None:
        for stmt in body:
            start, end = _own(stmt)
            items.append((start, end, depth, stmt))
            if importing:
                at_import.add(id(stmt))
            inner = importing and not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
            for name in ("body", "orelse", "finalbody"):
                walk(getattr(stmt, name, None) or [], depth + 1, inner)
            for handler in getattr(stmt, "handlers", None) or []:
                walk(handler.body, depth + 1, inner)
            for case in getattr(stmt, "cases", None) or []:
                walk(case.body, depth + 1, inner)

    walk(tree.body, 0, True)
    return _Spans(items, at_import)


def _own(stmt: ast.stmt) -> tuple[int, int]:
    """The lines of ``stmt`` itself: its header for a compound statement."""
    start = min([stmt.lineno, *[d.lineno for d in getattr(stmt, "decorator_list", None) or []]])
    body = getattr(stmt, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        return start, max(start, body[0].lineno - 1)
    return start, getattr(stmt, "end_lineno", None) or stmt.lineno


def _innermost(spans: _Spans, line: int) -> ast.stmt | None:
    best: tuple[int, ast.stmt] | None = None
    for start, end, depth, stmt in spans.items:
        if start <= line <= end and (best is None or depth > best[0]):
            best = (depth, stmt)
    if best is None:
        return None
    stmt = best[1]
    if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
        return stmt.body[0]  # ``try:`` itself runs no code; its first statement does
    return stmt


def _ran(stmt: ast.stmt, hits: set[int]) -> bool:
    start, end = _own(stmt)
    return any(start <= n <= end for n in hits)


def _proven(stmt: ast.stmt, spans: _Spans, tree: ast.Module, hits: set[int]) -> bool:
    if not _ran(stmt, hits):
        return False
    if id(stmt) not in spans.at_import or not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return True
    names = _bound(stmt)
    readers = [
        reader
        for reader in _function_reads(spans, tree, names)
        if reader is not stmt
    ]
    if not readers:
        return True  # nothing in this file reads it: running on import is all there is
    return any(_ran(reader, hits) for reader in readers)


def _bound(stmt: ast.stmt) -> set[str]:
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]  # type: ignore[attr-defined]
    out: set[str] = set()
    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                out.add(node.id)
    return out


def _function_reads(spans: _Spans, tree: ast.Module, names: set[str]) -> list[ast.stmt]:
    """Statements inside functions that read one of ``names`` (as a name or attribute)."""
    out: list[ast.stmt] = []
    for _start, _end, _depth, stmt in spans.items:
        if id(stmt) in spans.at_import:
            continue
        for node in _own_nodes(stmt):
            read = (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names) or (
                isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in names
            )
            if read:
                out.append(stmt)
                break
    return out


def _own_nodes(stmt: ast.stmt) -> Iterator[ast.AST]:
    """Nodes of ``stmt`` that are not inside a nested statement."""
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.stmt):
                stack.append(child)


def _is_code(lines: list[str], number: int) -> bool:
    if not 0 < number <= len(lines):
        return False
    text = lines[number - 1].strip()
    return bool(text) and not text.startswith("#")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
