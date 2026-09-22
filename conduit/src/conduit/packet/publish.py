"""Publish a migration packet into a catalog git checkout (by-package layout).

Does not open consumer PRs. Catalog write + optional commit/notice only.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from conduit.packet.cache import save_packet
from conduit.packet.validate import validate_packet

KNOWN_ECOSYSTEMS = ("pypi", "npm", "go", "maven", "other")


class PacketPublishError(Exception):
    """Packet could not be published into the catalog."""


@dataclass(frozen=True)
class CatalogPaths:
    """Relative paths written for one hop."""

    by_package: Path
    flat_root: Path | None = None


@dataclass
class PublishResult:
    """Outcome of writing one hop into a catalog tree."""

    packet_id: str
    catalog_root: Path
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    committed: bool = False
    commit_sha: str | None = None
    git_commands: list[str] = field(default_factory=list)
    notice_posted: bool = False


def _safe_path_token(value: str, *, label: str) -> str:
    text = (value or "").strip()
    if not text or "/" in text or "\\" in text or ".." in text or text in {".", ".."}:
        raise PacketPublishError(f"unsafe {label}: {value!r}")
    return text.replace(" ", "")


def catalog_paths_for_packet(packet: dict[str, Any]) -> CatalogPaths:
    """
    Canonical layout plus optional root mirror.

    Prefer ``by-package/<pkg>/<eco>/<packet_id>.json``. When ``packet_id``
    omits ``-<ecosystem>-`` (e.g. ``openai-0.28.1-1.0.0``), also mirror at
    ``<packet_id>.json`` so ``catalog_url_for_name`` slug fetch works.
    """
    package = _safe_path_token(str(packet.get("package") or ""), label="package")
    ecosystem = _safe_path_token(
        str(packet.get("ecosystem") or "").strip().lower(), label="ecosystem"
    )
    packet_id = _safe_path_token(str(packet.get("packet_id") or ""), label="packet_id")
    if ecosystem not in KNOWN_ECOSYSTEMS:
        raise PacketPublishError(f"unsupported ecosystem {ecosystem!r}")
    filename = f"{packet_id}.json"

    by_package = Path("by-package") / package / ecosystem / filename
    flat_root: Path | None = None
    for eco in KNOWN_ECOSYSTEMS:
        if f"-{eco}-" in packet_id:
            break
    else:
        flat_root = Path(filename)
    return CatalogPaths(by_package=by_package, flat_root=flat_root)


def _load_packet(packet: Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(packet, dict):
        return packet
    path = Path(packet)
    if not path.is_file():
        raise PacketPublishError(f"packet file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PacketPublishError(f"failed to read packet: {exc}") from exc
    if not isinstance(data, dict):
        raise PacketPublishError("packet JSON must be an object")
    return data


def _packets_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return json.dumps(a, sort_keys=True, separators=(",", ":")) == json.dumps(
        b, sort_keys=True, separators=(",", ":")
    )


def _is_git_repo(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _looks_like_git_url(raw: str) -> bool:
    parsed = urlparse(raw)
    if parsed.scheme in {"git", "http", "https", "ssh"}:
        return True
    return raw.startswith("git@")


def resolve_catalog_root(catalog: str) -> Path:
    """
    Resolve ``--catalog`` to a local directory.

    Local paths are used as-is. Git URLs are shallow-cloned into a temp dir
    (left on disk so the operator can push).
    """
    raw = (catalog or "").strip()
    if not raw:
        raise PacketPublishError("--catalog is required")

    if _looks_like_git_url(raw):
        tmp = Path(tempfile.mkdtemp(prefix="conduit-catalog-"))
        try:
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", raw, str(tmp)],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise PacketPublishError(f"git clone failed: {exc}") from exc
        if proc.returncode != 0:
            raise PacketPublishError(
                f"git clone failed: {(proc.stderr or proc.stdout or '').strip()}"
            )
        return tmp

    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise PacketPublishError(f"catalog path is not a directory: {root}")
    return root


def _git_commands_for(relpaths: list[str], packet_id: str) -> list[str]:
    quoted = " ".join(relpaths)
    return [
        f"git add {quoted}",
        f'git commit -m "Publish packet {packet_id}"',
        "git push",
    ]


def _try_commit(root: Path, relpaths: list[str], packet_id: str) -> tuple[bool, str | None]:
    try:
        add = subprocess.run(
            ["git", "-C", str(root), "add", "--", *relpaths],
            check=False,
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            return False, None
        commit = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "commit",
                "-m",
                f"Publish packet {packet_id}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            return False, None
        sha_proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else None
        return True, sha
    except OSError:
        return False, None


def post_notice(
    notice_url: str,
    *,
    packet: dict[str, Any],
    relpath: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """POST a small webhook body announcing the published hop (no consumer PRs)."""
    body = {
        "event": "conduit.packet.published",
        "packet_id": str(packet.get("packet_id") or ""),
        "package": str(packet.get("package") or ""),
        "ecosystem": str(packet.get("ecosystem") or ""),
        "from_version": str(packet.get("from_version") or ""),
        "to_version": str(packet.get("to_version") or ""),
        "path": relpath,
    }
    url = notice_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PacketPublishError(f"invalid --notice-url: {notice_url!r}")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise PacketPublishError(f"notice webhook failed: {exc}") from exc
    if resp.status_code < 200 or resp.status_code >= 300:
        raise PacketPublishError(
            f"notice webhook returned HTTP {resp.status_code}"
        )
    return body


def publish_packet(
    packet: Path | dict[str, Any] | str,
    catalog: str,
    *,
    notice_url: str | None = None,
    commit: bool = True,
) -> PublishResult:
    """
    Validate and write ``packet`` under the catalog ``by-package`` layout.

    Does not open consumer PRs. Second publish of the same hop is idempotent.
    """
    data = _load_packet(packet if not isinstance(packet, str) else Path(packet))
    errors = validate_packet(data)
    if errors:
        raise PacketPublishError("invalid packet:\n" + "\n".join(errors))

    root = resolve_catalog_root(catalog)
    paths = catalog_paths_for_packet(data)
    packet_id = str(data.get("packet_id") or "")
    targets: list[tuple[str, Path]] = [
        (paths.by_package.as_posix(), root / paths.by_package)
    ]
    if paths.flat_root is not None:
        targets.append((paths.flat_root.as_posix(), root / paths.flat_root))

    written: list[str] = []
    unchanged: list[str] = []
    for rel, dest in targets:
        if dest.is_file():
            try:
                existing = json.loads(dest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict) and _packets_equal(existing, data):
                unchanged.append(rel)
                continue
        save_packet(dest, data)
        written.append(rel)

    relpaths = [rel for rel, _ in targets]
    git_cmds = _git_commands_for(relpaths, packet_id)
    committed = False
    commit_sha: str | None = None
    if commit and written and _is_git_repo(root):
        committed, commit_sha = _try_commit(root, relpaths, packet_id)
    elif not commit:
        git_cmds = _git_commands_for(relpaths, packet_id)

    notice_posted = False
    if notice_url:
        post_notice(
            notice_url,
            packet=data,
            relpath=paths.by_package.as_posix(),
        )
        notice_posted = True

    return PublishResult(
        packet_id=packet_id,
        catalog_root=root,
        written=written,
        unchanged=unchanged,
        committed=committed,
        commit_sha=commit_sha,
        git_commands=[] if committed else git_cmds,
        notice_posted=notice_posted,
    )
