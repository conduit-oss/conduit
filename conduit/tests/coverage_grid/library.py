"""Two-version fake outside-service library for the coverage grid.

``zzgrid`` opens a real stdlib socket in its client constructor so the
evidence classifier treats it as an outside service — no name allowlist.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

DIST = "zzgrid"
IMPORT = "zzgrid"

# Shared surface present in both versions.
_COMMON = '''
import socket

class Response:
    def __init__(self, payload=None):
        self.payload = payload or {"ok": True}
        self.data = self.payload

    def get(self, key, default=None):
        return self.payload.get(key, default)

class Client:
    """Product client. Constructing it opens the network."""

    host = "api.zzgrid.test"

    def __init__(self, host=None):
        self.host = host or self.host
        # Classifier evidence: socket I/O owned by this distribution.
        self._sock = socket.create_connection((self.host, 443), timeout=0.001)

    def ping(self):
        return Response({"ok": True})

    def fetch(self, path="/"):
        return Response({"path": path})

def connect(host=None):
    return Client(host=host)

def factory():
    return Client()

REGISTRY = {"default": Client}

class Base:
    def run(self):
        return Client().ping()
'''

# v1 has methods that v2 removes / changes.
_V1_EXTRA = '''
class Client(Client):  # type: ignore[no-redef]
    def legacy_call(self, path="/", *, mode="fast"):
        return Response({"path": path, "mode": mode})

    def rename_me(self):
        return Response()

    def needs_new_arg(self):
        return Response()

    def drop_kw(self, *, token="x"):
        return Response({"token": token})

    def dict_shape(self):
        return {"items": [1]}

    def attr_holder(self):
        return Response({"gone": 1})

    def become_class(self):
        return Response()

    def soft_default(self, n=1):
        return Response({"n": n})
'''

# Flatten: rewrite Client as single class for v1/v2 files.
_V1 = '''
import socket

class Response:
    def __init__(self, payload=None):
        self.payload = payload or {"ok": True}
        self.data = self.payload
        self.gone = 1

    def get(self, key, default=None):
        return self.payload.get(key, default)

class Client:
    host = "api.zzgrid.test"

    def __init__(self, host=None):
        self.host = host or self.host
        try:
            self._sock = socket.create_connection((self.host, 443), timeout=0.001)
        except OSError:
            self._sock = None

    def ping(self):
        return Response({"ok": True})

    def fetch(self, path="/"):
        return Response({"path": path})

    def legacy_call(self, path="/", *, mode="fast"):
        return Response({"path": path, "mode": mode})

    def rename_me(self):
        return Response()

    def needs_new_arg(self):
        return Response()

    def drop_kw(self, *, token="x"):
        return Response({"token": token})

    def dict_shape(self):
        return {"items": [1]}

    def attr_holder(self):
        return Response({"gone": 1})

    def become_class(self):
        return Response()

    def soft_default(self, n=1):
        return Response({"n": n})

def connect(host=None):
    return Client(host=host)

def factory():
    return Client()

REGISTRY = {"default": Client}

class Base:
    def run(self):
        return Client().ping()
'''

_V2 = '''
import socket

class Response:
    def __init__(self, payload=None):
        self.payload = payload or {"ok": True}
        self.data = self.payload

    def get(self, key, default=None):
        return self.payload.get(key, default)

class Client:
    host = "api.zzgrid.test"

    def __init__(self, host=None):
        self.host = host or self.host
        try:
            self._sock = socket.create_connection((self.host, 443), timeout=0.001)
        except OSError:
            self._sock = None

    def ping(self):
        return Response({"ok": True})

    def fetch(self, path="/"):
        return Response({"path": path})

    # legacy_call removed
    def renamed(self):  # was rename_me
        return Response()

    def needs_new_arg(self, required):
        return Response({"required": required})

    def drop_kw(self):  # token kw removed
        return Response()

    class DictShapeResult:
        def __init__(self):
            self.items = [1]

    def dict_shape(self):
        return Client.DictShapeResult()

    def attr_holder(self):
        return Response({"ok": True})  # .gone attribute gone

    class BecomeClass:
        pass

    become_class = BecomeClass  # kind: function -> class

    def soft_default(self, n=2):  # default changed (deprecation-only)
        return Response({"n": n})

def connect(host=None):
    return Client(host=host)

def factory():
    return Client()

REGISTRY = {"default": Client}

class Base:
    def run(self):
        return Client().ping()
'''


def write_wheels(dest: Path) -> tuple[Path, Path]:
    dest.mkdir(parents=True, exist_ok=True)
    old = dest / f"{DIST}-1.0.0-py3-none-any.whl"
    new = dest / f"{DIST}-2.0.0-py3-none-any.whl"
    _write_wheel(old, "1.0.0", _V1)
    _write_wheel(new, "2.0.0", _V2)
    return old, new


def write_index(fixtures: Path, old: Path, new: Path) -> Path:
    import json

    fixtures.mkdir(parents=True, exist_ok=True)
    wheels = fixtures / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    # copy/link into fixtures layout expected by impact --fixtures
    for src in (old, new):
        target = wheels / src.name
        if not target.exists():
            target.write_bytes(src.read_bytes())
    index = {
        "packages": {
            DIST: {
                "from": "1.0.0",
                "to": "2.0.0",
                "wheels": {
                    "1.0.0": f"wheels/{old.name}",
                    "2.0.0": f"wheels/{new.name}",
                },
            }
        }
    }
    path = fixtures / "index.json"
    path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return path


def _write_wheel(path: Path, version: str, source: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{DIST}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{DIST}-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: coverage-grid\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{DIST}-{version}.dist-info/top_level.txt",
            f"{IMPORT}\n",
        )
        archive.writestr(f"{IMPORT}/__init__.py", source)


def install_v1_into_repo(repo: Path) -> None:
    """Pin v1 into a synthetic ``.venv`` so LibraryTypes / pins resolve."""
    site = repo / ".venv" / "lib" / "python3.12" / "site-packages"
    info = site / f"{DIST}-1.0.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        f"Name: {DIST}\nVersion: 1.0.0\n", encoding="utf-8"
    )
    (info / "top_level.txt").write_text(f"{IMPORT}\n", encoding="utf-8")
    pkg = site / IMPORT
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(_V1, encoding="utf-8")
    (repo / "requirements.txt").write_text(f"{DIST}==1.0.0\n", encoding="utf-8")
