"""Optional receiver types from the Pyright language server.

The hand-written resolver stays in place when this server is not installed,
when ``CONDUIT_TYPES=native``, or when the answer is Any, Unknown, or None.
Pyright is a Node program. A pip install cannot vendor Node, so CI and a
user environment need Node on ``PATH`` and ``pyright-langserver`` (or
``CONDUIT_PYRIGHT`` pointing at ``pyright-langserver.js``).
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import urllib.parse
from pathlib import Path

_CLASS_LINE = re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_UNKNOWN = frozenset("any unknown none nonetype".split())
STATS = {"queries": 0, "concrete": 0, "disabled": False}


def reset_stats() -> None:
    STATS["queries"] = 0
    STATS["concrete"] = 0
    STATS["disabled"] = False


def typecheck_wanted() -> bool:
    """True when this process should ask Pyright before the native resolver."""
    mode = os.environ.get("CONDUIT_TYPES", "").strip().lower()
    if mode in {"native", "off", "0", "false"}:
        return False
    if mode == "pyright":
        return True
    # Unset stays native so a machine with Node does not change results until
    # the checker is selected. ``auto`` uses it when the server can be started.
    if mode == "auto":
        return langserver_command() is not None
    return False


def langserver_command() -> list[str] | None:
    explicit = os.environ.get("CONDUIT_PYRIGHT", "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file() and path.suffix == ".js":
            node = shutil.which("node")
            if node:
                return [node, str(path), "--stdio"]
        if path.is_file():
            return [str(path), "--stdio"]
        return None
    found = shutil.which("pyright-langserver")
    if found:
        return [found, "--stdio"]
    return None


def open_session(root: Path) -> PyrightSession | None:
    if not typecheck_wanted():
        return None
    if langserver_command() is None:
        return None
    try:
        return PyrightSession(root)
    except (OSError, TimeoutError, RuntimeError):
        return None


def qualname_from_uri(uri: str, class_name: str) -> str | None:
    """``site-packages`` path plus ``class Name`` becomes a dotted qualname."""
    if not class_name or not class_name.isidentifier():
        return None
    path = _uri_path(uri)
    if path is None:
        return None
    parts = path.parts
    marker = ""
    for name in ("site-packages", "dist-packages"):
        if name in parts:
            marker = name
    if not marker:
        return None
    index = len(parts) - 1 - parts[::-1].index(marker)
    rel = list(parts[index + 1 :])
    if not rel:
        return None
    last = rel[-1]
    if last.endswith(".py"):
        rel[-1] = last[:-3]
    elif last.endswith(".pyi"):
        rel[-1] = last[:-4]
    else:
        return None
    if rel[-1] == "__init__":
        rel = rel[:-1]
    module = ".".join(part for part in rel if part)
    if not module:
        return None
    return f"{module}.{class_name}"


def hover_is_only_unknown(text: str) -> bool:
    """True when the hover names no type other than Any, Unknown, or None.

    The type is the text after the colon. A union that also names a class is
    concrete. An empty hover is not an answer, so the caller still reads the
    definition location.
    """
    if not text or not text.strip():
        return False
    cleaned = text.replace("`", " ")
    typed = ""
    for line in cleaned.splitlines():
        if ":" not in line:
            continue
        side = line.rsplit(":", 1)[-1]
        if re.search(r"[A-Za-z_]", side):
            typed = side
            break
    if not typed:
        return False
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", typed)
    if not tokens:
        return False
    return all(token.lower() in _UNKNOWN for token in tokens)


class PyrightSession:
    def __init__(self, root: Path) -> None:
        command = langserver_command()
        if not command:
            raise RuntimeError("language server is not installed")
        self.root = root.resolve()
        self.disabled = False
        self._id = 0
        self._opened: set[str] = set()
        self._cache: dict[tuple[str, int, int], tuple[str, ...]] = {}
        self._config = _write_config(self.root)
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            self._request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": _path_uri(self.root),
                    "workspaceFolders": [{"uri": _path_uri(self.root), "name": self.root.name}],
                    "capabilities": {
                        "textDocument": {
                            "hover": {"contentFormat": ["plaintext", "markdown"]},
                            "typeDefinition": {"linkSupport": True},
                        }
                    },
                },
                timeout=60,
            )
            self._notify("initialized", {})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.disabled = True
        proc = getattr(self, "proc", None)
        if proc is not None and proc.poll() is None:
            try:
                self._notify("shutdown", {})
                self._notify("exit", {})
            except (OSError, TimeoutError, RuntimeError):
                pass
            try:
                proc.kill()
            except OSError:
                pass
        config = getattr(self, "_config", None)
        if config is not None:
            try:
                config.unlink()
            except OSError:
                pass
            self._config = None

    def classes_at(self, path: Path, line: int, col: int) -> tuple[str, ...]:
        """Qualnames for the expression at a 1-indexed line and 0-indexed column."""
        if self.disabled or self.proc.poll() is not None:
            self.disabled = True
            return ()
        key = (str(path), line, col)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            found = self._classes_at(path, line, col)
        except (OSError, TimeoutError, RuntimeError, json.JSONDecodeError):
            self.disabled = True
            found = ()
        STATS["queries"] += 1
        if found:
            STATS["concrete"] += 1
        if self.disabled:
            STATS["disabled"] = True
        self._cache[key] = found
        return found

    def _classes_at(self, path: Path, line: int, col: int) -> tuple[str, ...]:
        uri = _path_uri(path)
        self._open(path, uri)
        position = {"line": max(line - 1, 0), "character": max(col, 0)}
        params = {"textDocument": {"uri": uri}, "position": position}
        hover = self._request("textDocument/hover", params, timeout=20)
        if hover_is_only_unknown(_hover_text(hover)):
            return ()
        defined = self._request("textDocument/typeDefinition", params, timeout=20)
        classes: list[str] = []
        for loc_uri, loc_line in _locations(defined):
            name = _class_name_on(loc_uri, loc_line)
            qual = qualname_from_uri(loc_uri, name) if name else None
            if qual and qual not in classes:
                classes.append(qual)
        return tuple(classes)

    def _open(self, path: Path, uri: str) -> None:
        if uri in self._opened:
            return
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            text = ""
        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                }
            },
        )
        self._opened.add(uri)

    def _request(self, method: str, params: dict, timeout: float) -> object:
        self._id += 1
        ident = self._id
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        deadline = _now() + timeout
        while True:
            remaining = deadline - _now()
            if remaining <= 0:
                raise TimeoutError(method)
            message = self._read(remaining)
            if message.get("id") == ident:
                if "error" in message and message["error"]:
                    return None
                return message.get("result")

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, payload: dict) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("server stdin is closed")
        body = json.dumps(payload).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        self.proc.stdin.write(frame)
        self.proc.stdin.flush()

    def _read(self, timeout: float) -> dict:
        if self.proc.stdout is None:
            raise RuntimeError("server stdout is closed")
        header = self._read_until(b"\r\n\r\n", timeout)
        length = 0
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        if length <= 0:
            raise RuntimeError("language server sent no content length")
        body = self._read_exact(length, timeout)
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise RuntimeError("language server sent a non-object")
        return parsed

    def _read_until(self, marker: bytes, timeout: float) -> bytes:
        data = b""
        while marker not in data:
            data += self._read_exact(1, timeout)
            if len(data) > 65536:
                raise RuntimeError("language server header is too large")
        return data

    def _read_exact(self, count: int, timeout: float) -> bytes:
        if self.proc.stdout is None:
            raise RuntimeError("server stdout is closed")
        fd = self.proc.stdout.fileno()
        deadline = _now() + timeout
        chunks: list[bytes] = []
        got = 0
        while got < count:
            remaining = deadline - _now()
            if remaining <= 0:
                raise TimeoutError("language server read")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise TimeoutError("language server read")
            piece = os.read(fd, count - got)
            if not piece:
                raise RuntimeError("language server closed stdout")
            chunks.append(piece)
            got += len(piece)
        return b"".join(chunks)


def _write_config(root: Path) -> Path | None:
    """Point the server at ``CONDUIT_PYTHON`` without leaving a config behind.

    An existing project config is left alone. The file this writes is removed
    when the session closes.
    """
    dest = root / "pyrightconfig.json"
    if dest.exists():
        return None
    python = os.environ.get("CONDUIT_PYTHON", "").strip()
    if not python:
        return None
    # The interpreter is often a symlink into the base install. Keep the
    # venv path itself so the server reads that environment's site-packages.
    venv = Path(python)
    if not venv.is_absolute():
        venv = venv.absolute()
    if venv.parent.name != "bin":
        return None
    folder = venv.parent.parent
    payload = {
        "venvPath": str(folder.parent),
        "venv": folder.name,
        "useLibraryCodeForTypes": True,
    }
    dest.write_text(json.dumps(payload), encoding="utf-8")
    return dest


def _hover_text(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    contents = result.get("contents")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        value = contents.get("value")
        return value if isinstance(value, str) else ""
    if isinstance(contents, list):
        parts: list[str] = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("value"), str):
                parts.append(item["value"])
        return "\n".join(parts)
    return ""


def _locations(result: object) -> list[tuple[str, int]]:
    if result is None:
        return []
    items = result if isinstance(result, list) else [result]
    found: list[tuple[str, int]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri") or item.get("targetUri") or ""
        if not isinstance(uri, str) or not uri:
            continue
        span = item.get("range") or item.get("targetSelectionRange") or item.get("targetRange") or {}
        start = span.get("start") if isinstance(span, dict) else {}
        line = start.get("line") if isinstance(start, dict) else 0
        found.append((uri, int(line or 0)))
    return found


def _class_name_on(uri: str, line: int) -> str:
    """Class name at a 0-indexed line, looking a few lines up for ``class``."""
    path = _uri_path(uri)
    if path is None or not path.is_file():
        return ""
    try:
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start = min(max(line, 0), len(rows) - 1) if rows else 0
    for index in range(start, max(-1, start - 40), -1):
        match = _CLASS_LINE.match(rows[index])
        if match:
            return match.group(1)
    return ""


def _uri_path(uri: str) -> Path | None:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(urllib.parse.unquote(parsed.path))


def _path_uri(path: Path) -> str:
    return "file://" + urllib.parse.quote(str(path.resolve()))


def _now() -> float:
    import time

    return time.monotonic()
