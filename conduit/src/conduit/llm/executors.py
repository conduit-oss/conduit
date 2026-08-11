"""Execute Conduit function tools against a consumer repo."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from conduit.prune.grep_imports import SKIP_DIRS
from conduit.repair_ignore import IgnoreList

LogFn = Callable[[str], None]

# Allowlisted local shell shapes (consumer cwd only).
_SHELL_ALLOW: list[re.Pattern[str]] = [
    re.compile(r"^pytest(\s|$)", re.I),
    re.compile(r"^python(\s+-m)?\s+pytest(\s|$)", re.I),
    re.compile(r"^python\s+-c\s+", re.I),
    re.compile(r"^py\s+-c\s+", re.I),
    re.compile(r"^pip\s+(show|list)(\s|$)", re.I),
    re.compile(r"^python\s+-m\s+pip\s+(show|list)(\s|$)", re.I),
]


def _noop_log(_: str) -> None:
    return None


def shell_command_allowed(command: str) -> bool:
    cmd = (command or "").strip()
    if not cmd or len(cmd) > 2000:
        return False
    # Reject shell chaining / substitution / pipes.
    if any(tok in cmd for tok in ("&&", "||", "|", "`", "$(", "\n", "\r")):
        return False
    return any(p.search(cmd) for p in _SHELL_ALLOW)


@dataclass
class RepoToolExecutor:
    """Local tool executor for Responses function_call items."""

    root: Path
    ignore: IgnoreList = field(default_factory=IgnoreList)
    allow_writes: bool = False
    allow_run_tests: bool = False
    allow_shell: bool = False
    log: LogFn = field(default=_noop_log)
    max_file_chars: int = 80_000
    max_fetch_chars: int = 24_000
    max_shell_chars: int = 12_000
    shell_timeout_s: float = 120.0
    written_files: list[str] = field(default_factory=list)

    def __call__(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            if name == "list_files":
                return self._list_files(arguments)
            if name == "read_file":
                return self._read_file(arguments)
            if name == "write_file":
                return self._write_file(arguments)
            if name == "run_tests":
                return self._run_tests()
            if name == "run_shell":
                return self._run_shell(arguments)
            if name == "grep":
                return self._grep(arguments)
            if name == "fetch_url":
                return self._fetch_url(arguments)
            if name == "apply_patch":
                return self._apply_patch(arguments)
            return json.dumps({"error": f"unknown tool {name!r}"})
        except Exception as exc:  # noqa: BLE001 — surface to model
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    def _resolve(self, rel: str) -> Path:
        rel_posix = rel.replace("\\", "/").lstrip("/")
        if not rel_posix or rel_posix.startswith("..") or "/../" in f"/{rel_posix}/":
            raise ValueError(f"invalid path: {rel!r}")
        path = (self.root / rel_posix).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise ValueError(f"path escapes repo root: {rel!r}")
        return path

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def _list_files(self, args: dict[str, Any]) -> str:
        directory = str(args.get("directory") or ".")
        pattern = str(args.get("glob") or "**/*")
        limit = int(args.get("limit") or 80)
        limit = max(1, min(limit, 200))
        base = self._resolve(directory)
        if not base.is_dir():
            return json.dumps({"error": f"not a directory: {directory}"})
        out: list[str] = []
        for path in sorted(base.glob(pattern)):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                rel = self._rel(path)
            except ValueError:
                continue
            if self.ignore.path_ignored(rel):
                continue
            out.append(rel)
            if len(out) >= limit:
                break
        return json.dumps({"files": out, "count": len(out)})

    def _read_file(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path") or "")
        path = self._resolve(rel)
        if self.ignore.path_ignored(self._rel(path)):
            return json.dumps({"error": f"path ignored: {rel}"})
        if not path.is_file():
            return json.dumps({"error": f"not a file: {rel}"})
        text = path.read_text(encoding="utf-8")
        truncated = len(text) > self.max_file_chars
        if truncated:
            text = text[: self.max_file_chars]
        return json.dumps(
            {"path": self._rel(path), "contents": text, "truncated": truncated}
        )

    def _write_file(self, args: dict[str, Any]) -> str:
        if not self.allow_writes:
            return json.dumps({"error": "write_file not allowed in this mode"})
        rel = str(args.get("path") or "")
        contents = args.get("contents")
        if not isinstance(contents, str):
            return json.dumps({"error": "contents must be a string"})
        path = self._resolve(rel)
        rel_posix = self._rel(path) if path.exists() else rel.replace("\\", "/")
        if self.ignore.path_ignored(rel_posix):
            return json.dumps({"error": f"path ignored: {rel_posix}"})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        written = self._rel(path)
        if written not in self.written_files:
            self.written_files.append(written)
        self.log(f"[llm-tool] write_file {written}")
        return json.dumps({"ok": True, "path": written, "bytes": len(contents)})

    def _run_tests(self) -> str:
        if not self.allow_run_tests:
            return json.dumps({"error": "run_tests not allowed in this mode"})
        from conduit.test_runner import run_tests

        self.log("[llm-tool] run_tests")
        result = run_tests(self.root)
        return json.dumps(
            {
                "passed": result.passed,
                "returncode": result.returncode,
                "runner": result.runner,
                "command": result.command,
                "stdout": (result.stdout or "")[-8000:],
                "stderr": (result.stderr or "")[-4000:],
                "summary": result.summary,
            }
        )

    def _run_shell(self, args: dict[str, Any]) -> str:
        if not self.allow_shell:
            return json.dumps({"error": "run_shell not allowed in this mode"})
        command = str(args.get("command") or "").strip()
        if not shell_command_allowed(command):
            return json.dumps(
                {
                    "error": "command not allowlisted",
                    "hint": "Allowed: pytest, python -m pytest, python -c, pip show/list",
                    "command": command,
                }
            )
        self.log(f"[llm-tool] run_shell {command[:120]}")
        try:
            # Prefer list argv when possible; fall back to shell=False with shlex.
            try:
                argv = shlex.split(command, posix=os_name_is_posix())
            except ValueError:
                argv = shlex.split(command)
            completed = subprocess.run(
                argv,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=self.shell_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return json.dumps(
                {"error": "timeout", "command": command, "timeout_s": self.shell_timeout_s}
            )
        except OSError as exc:
            return json.dumps({"error": f"OSError: {exc}", "command": command})

        stdout = (completed.stdout or "")[-self.max_shell_chars :]
        stderr = (completed.stderr or "")[- self.max_shell_chars // 2 :]
        return json.dumps(
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        )

    def _grep(self, args: dict[str, Any]) -> str:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return json.dumps({"error": "pattern required"})
        directory = str(args.get("directory") or ".")
        glob_pat = str(args.get("glob") or "**/*")
        limit = max(1, min(int(args.get("limit") or 40), 200))
        flags = re.I if args.get("case_insensitive") else 0
        try:
            cre = re.compile(pattern, flags)
        except re.error as exc:
            return json.dumps({"error": f"invalid regex: {exc}"})

        base = self._resolve(directory)
        if not base.is_dir():
            return json.dumps({"error": f"not a directory: {directory}"})

        matches: list[dict[str, Any]] = []
        for path in sorted(base.glob(glob_pat)):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                rel = self._rel(path)
            except ValueError:
                continue
            if self.ignore.path_ignored(rel):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if cre.search(line):
                    matches.append(
                        {"path": rel, "line": i, "text": line[:400]}
                    )
                    if len(matches) >= limit:
                        return json.dumps(
                            {"matches": matches, "count": len(matches), "truncated": True}
                        )
        return json.dumps({"matches": matches, "count": len(matches), "truncated": False})

    def _fetch_url(self, args: dict[str, Any]) -> str:
        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return json.dumps({"error": "url must be http(s)"})
        from conduit.context.fetch import fetch_url

        self.log(f"[llm-tool] fetch_url {url}")
        try:
            text = fetch_url(url)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"fetch failed: {exc}", "url": url})
        truncated = len(text) > self.max_fetch_chars
        if truncated:
            text = text[: self.max_fetch_chars]
        return json.dumps({"url": url, "text": text, "truncated": truncated})

    def _apply_patch(self, args: dict[str, Any]) -> str:
        """Prefer write_file; accept path+contents as a simple patch shape."""
        if not self.allow_writes:
            return json.dumps({"error": "apply_patch not allowed in this mode"})
        if args.get("path") and isinstance(args.get("contents"), str):
            return self._write_file(args)
        return json.dumps(
            {
                "error": "unified diff apply_patch not supported; use write_file",
                "hint": "Provide write_file(path, contents) with full file contents.",
            }
        )


def os_name_is_posix() -> bool:
    import os

    return os.name == "posix"
