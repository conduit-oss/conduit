"""Install bumped packages into the active interpreter before verify."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def bumped_package_specs(packet: dict[str, Any]) -> list[str]:
    """Return ``package==to_version`` pins for DEPENDENCY_BUMP rules."""
    specs: list[str] = []
    seen: set[str] = set()
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "DEPENDENCY_BUMP":
            continue
        eco = str(rule.get("ecosystem") or "").lower()
        ecosystems = rule.get("ecosystems")
        if isinstance(ecosystems, list):
            ecos = {str(e).lower() for e in ecosystems}
        else:
            ecos = {eco} if eco else set()
        # Prefer pip/pyproject bumps; skip pure npm when mixed.
        if ecos and not (ecos & {"pip", "pyproject", "pypi", ""}):
            if ecos <= {"npm", "yarn", "pnpm", "node"}:
                continue
        pkg = str(rule.get("package") or "").strip()
        ver = str(rule.get("to_version") or "").strip().lstrip("=")
        if not pkg or not ver:
            continue
        key = pkg.lower()
        if key in seen:
            continue
        seen.add(key)
        specs.append(f"{pkg}=={ver}")
    return specs


def sync_bumped_packages(
    packet: dict[str, Any],
    *,
    python: str | None = None,
    log=None,
) -> list[str]:
    """
    ``pip install package==to`` for each DEPENDENCY_BUMP so verify uses the
    migrated SDK, not a stale site-packages pin.
    """
    specs = bumped_package_specs(packet)
    if not specs:
        return []
    exe = python or sys.executable
    cmd = [exe, "-m", "pip", "install", "--upgrade", *specs]
    if log:
        log(f"Installing bumped packages: {' '.join(specs)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if log:
            log(f"[yellow]pip install failed:[/yellow] {exc}")
        return specs
    if proc.returncode != 0 and log:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = err[-3:] if err else ["(no output)"]
        log(f"[yellow]pip install exit {proc.returncode}:[/yellow] {' '.join(tail)}")
    return specs
