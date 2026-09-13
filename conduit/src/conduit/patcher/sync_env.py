"""Install bumped packages into the consumer verify interpreter before verify."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class VerifyEnvError(RuntimeError):
    """Consumer verify env cannot be installed (abort migration early)."""


_CONFLICT_MARKERS = (
    "resolutionimpossible",
    "conflicting dependencies",
    "cannot install",
    "dependency conflict",
)


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


def consumer_req_files(root: Path) -> list[Path]:
    """Prefer root ``requirements.txt``, then shallow pip manifests."""
    root = root.resolve()
    primary = root / "requirements.txt"
    if primary.is_file():
        return [primary]
    try:
        from conduit.detect.pip_manifests import iter_pip_manifests

        found = [
            p
            for p in iter_pip_manifests(root, scope="main")
            if p.name.lower() == "requirements.txt"
        ]
        if found:
            return [found[0]]
    except Exception:
        pass
    return []


def _import_name(spec: str) -> str:
    name = spec.split("==", 1)[0].strip()
    return name.replace("-", "_")


_PROGRESS_PREFIXES = (
    "collecting ",
    "downloading ",
    "using cached",
    "installing collected",
    "building wheel",
    "created wheel",
    "stored in",
    "obtaining ",
    "looking in indexes",
    "requirement already satisfied",
    "preparing metadata",
)

_ERROR_HINTS = (
    "error:",
    "exception",
    "traceback",
    "failed",
    "could not",
    "no matching distribution",
    "resolutionimpossible",
    "conflicting dependencies",
    "cannot install",
    "dependency conflict",
    "the conflict is caused by",
    "depends on ",
    "the user requested ",
)

_DEPENDS_ON_RE = re.compile(
    r"(?i)^\s*(?P<pkg>[A-Za-z0-9_.\-]+)\s+(?P<ver>[^\s]+)\s+depends on\s+(?P<req>.+?)\s*$"
)
_USER_REQUESTED_RE = re.compile(
    r"(?i)^\s*the user requested\s+(?P<spec>[A-Za-z0-9_.\-]+(?:\s*[=<>!~]=?\s*[^\s]+)?)\s*$"
)
# pip: Cannot install -r PATH (line N) and openai==3.3.1 because …
_LINE_CONFLICT_RE = re.compile(
    r"(?is)cannot install\s+-r\s+(?P<path>.+?)\s+\(line\s+(?P<line>\d+)\)"
    r"\s+and\s+(?P<spec>[A-Za-z0-9_.\-]+(?:\s*[=<>!~]=?\s*[^,\s]+)?)"
)


def resolve_requirements_line(path: str | Path, lineno: int) -> str | None:
    """Return the pin text at 1-based ``lineno`` in a requirements file."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if lineno < 1 or lineno > len(lines):
        return None
    raw = lines[lineno - 1].strip()
    if not raw or raw.startswith("#"):
        return None
    # Drop inline comments; keep the requirement token(s).
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw or None


def parse_line_conflict(detail: str) -> tuple[str, int, str] | None:
    """Parse ``(path, lineno, conflicting_spec)`` from a pip line-conflict ERROR."""
    m = _LINE_CONFLICT_RE.search(detail or "")
    if not m:
        return None
    path = m.group("path").strip().strip("\"'")
    try:
        lineno = int(m.group("line"))
    except ValueError:
        return None
    spec = re.sub(r"\s+", "", m.group("spec").strip())
    if not path or lineno < 1 or not spec:
        return None
    return path, lineno, spec



def _is_noise_line(line: str) -> bool:
    low = (line or "").strip().lower()
    if not low:
        return True
    if low.startswith("[notice]"):
        return True
    if "a new release of pip" in low or "to update, run:" in low:
        return True
    if low.startswith("ignoring ") and "markers" in low:
        return True
    if "for help visit http" in low:
        return True
    if any(low.startswith(p) for p in _PROGRESS_PREFIXES):
        return True
    # Progress bars / byte counters
    if re.match(r"^[\-─=\s\d\.]+(?:%[|\s]|mb/s|kb/s|b/s)", low):
        return True
    if re.match(r"^\d+(\.\d+)?\s*/\s*\d+", low):
        return True
    return False


def _is_errorish_line(line: str) -> bool:
    low = (line or "").strip().lower()
    return any(h in low for h in _ERROR_HINTS)


def format_pip_failure(blob: str, *, max_lines: int = 16) -> str:
    """Keep real ERROR/conflict lines; drop Collecting/Downloading progress noise."""
    lines = [ln.rstrip() for ln in (blob or "").splitlines() if ln.strip()]
    if not lines:
        return "(no pip output)"

    errorish = [ln for ln in lines if _is_errorish_line(ln) and not _is_noise_line(ln)]
    if errorish:
        # Include a little context around the first errorish hit when present.
        start = next(
            (i for i, ln in enumerate(lines) if _is_errorish_line(ln)), 0
        )
        chunk = [
            ln
            for ln in lines[start : start + max_lines + 12]
            if not _is_noise_line(ln)
        ][:max_lines]
        return "\n".join(chunk) if chunk else "\n".join(errorish[:max_lines])

    useful = [ln for ln in lines if not _is_noise_line(ln)]
    if useful:
        picked = useful[-max_lines:]
        return (
            "pip failed (no ERROR line captured):\n" + "\n".join(picked)
        )
    return "pip failed (no ERROR line captured):\n" + "\n".join(lines[-6:])


def is_dep_conflict(detail: str) -> bool:
    low = (detail or "").lower()
    return any(m in low for m in _CONFLICT_MARKERS)


def parse_dep_blockers(detail: str) -> list[tuple[str, str, str]]:
    """Return ``(package, version, requirement)`` from pip conflict text."""
    hits: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ln in (detail or "").splitlines():
        m = _DEPENDS_ON_RE.match(ln.strip())
        if not m:
            continue
        row = (
            m.group("pkg").strip(),
            m.group("ver").strip(),
            m.group("req").strip(),
        )
        key = (row[0].lower(), row[1], row[2].lower())
        if key in seen:
            continue
        seen.add(key)
        hits.append(row)
    return hits


def parse_user_requested_specs(detail: str) -> list[str]:
    specs: list[str] = []
    seen: set[str] = set()
    for ln in (detail or "").splitlines():
        m = _USER_REQUESTED_RE.match(ln.strip())
        if not m:
            continue
        spec = re.sub(r"\s+", "", m.group("spec").strip())
        key = spec.lower()
        if key in seen:
            continue
        seen.add(key)
        specs.append(spec)
    return specs


def explain_dep_conflict(detail: str, *, specs: list[str] | None = None) -> str:
    """Plain-English summary for ResolutionImpossible / pin conflicts."""
    migrated = list(specs or [])
    if not migrated:
        migrated = parse_user_requested_specs(detail)
    blockers = parse_dep_blockers(detail)

    # Prefer blockers that mention a migrated package name.
    migrated_names = {
        s.split("==", 1)[0].split(">=", 1)[0].split("<=", 1)[0].strip().lower()
        for s in migrated
        if s.strip()
    }
    relevant = [
        b
        for b in blockers
        if any(name and name in b[2].lower() for name in migrated_names)
    ] or blockers

    lines = ["Dependency conflict while installing verify-venv."]
    if migrated:
        lines.append("Migrated pin: " + ", ".join(migrated))
    if relevant:
        for pkg, ver, req in relevant:
            lines.append(f"Blocked by: {pkg} {ver} (requires {req})")
        lines.append(
            "These cannot be installed together. Bump or relax the blocker "
            "so it allows the migrated package version, then re-run."
        )
        return "\n".join(lines)

    # Pip often only prints "Cannot install -r PATH (line N) and openai==…"
    # without the "depends on" breakdown — resolve line N from the file.
    line_hit = parse_line_conflict(detail)
    if line_hit is not None:
        req_path, lineno, other_spec = line_hit
        pin = resolve_requirements_line(req_path, lineno)
        try:
            rel = Path(req_path).name
        except Exception:
            rel = str(req_path)
        if pin:
            lines.append(f"Blocked by {rel} line {lineno}: {pin}")
        else:
            lines.append(
                f"Blocked by {rel} line {lineno} "
                f"(conflicts with {other_spec}; could not read pin text)"
            )
        if other_spec and other_spec not in migrated:
            lines.append(f"Conflicts with: {other_spec}")
        lines.append(
            "These cannot be installed together. Bump or relax the blocker "
            "so it allows the migrated package version, then re-run."
        )
        return "\n".join(lines)

    trimmed = format_pip_failure(detail)
    lines.append("Dependency conflict (could not parse blocker):")
    lines.append(trimmed)
    return "\n".join(lines)


def format_verify_env_failure(detail: str, *, specs: list[str] | None = None) -> str:
    """User-facing verify-env abort body (conflict summary or trimmed pip errors)."""
    pins = ", ".join(specs) if specs else "(no DEPENDENCY_BUMP pins)"
    body = (detail or "").strip() or "(no pip output)"
    if is_dep_conflict(body):
        return explain_dep_conflict(body, specs=list(specs or []))
    trimmed = format_pip_failure(body)
    if (
        "do not match the hashes" in body.lower()
        or "don't match the hashes" in body.lower()
        or "hashes are required" in body.lower()
        or "must have their versions pinned" in body.lower()
    ):
        return (
            "Cannot install consumer requirements into verify-venv "
            f"(migrated pins: {pins}).\n"
            "Requirement pin has stale or missing --hash= values (or missing "
            "transitive == pins) for pip --require-hashes. Re-run apply so "
            "Conduit refreshes the Poetry lock/export or hashed transitive "
            "pins, or re-export the lockfile.\n"
            f"{trimmed}"
        )
    return (
        "Cannot install consumer requirements into verify-venv "
        f"(migrated pins: {pins}).\n{trimmed}"
    )


def _run_pip(
    exe: str,
    args: list[str],
    *,
    log=None,
    label: str = "pip install",
    timeout: float = 600,
) -> tuple[bool, str]:
    from conduit.pulse import beat

    cmd = [exe, "-m", "pip", *args]
    if log:
        log(f"{label}: {' '.join(args)}")
        log(f"[dim]Into interpreter: {exe}[/dim]")
    beat("wait")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = str(exc)
        if log:
            log(f"[red]{label} failed:[/red] {detail}")
        return False, detail
    if proc.returncode != 0:
        blob = (proc.stderr or "") + "\n" + (proc.stdout or "")
        detail = format_pip_failure(blob)
        if log:
            log(f"[red]{label} exit {proc.returncode}:[/red]\n{detail}")
        return False, detail
    return True, ""


def _upgrade_pip(exe: str, *, log=None) -> None:
    _run_pip(
        exe,
        ["install", "--upgrade", "pip"],
        log=log,
        label="pip upgrade",
        timeout=180,
    )


def _pip_install_consumer_reqs(
    exe: str,
    root: Path,
    *,
    force: bool = False,
    log=None,
) -> tuple[bool, str]:
    """Install consumer requirements.txt (and optional pyproject) into verify env."""
    reqs = consumer_req_files(root)
    details: list[str] = []
    ok = True
    for req in reqs:
        try:
            rel = req.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = req.name
        args = ["install", "--upgrade"]
        if force:
            args.extend(["--force-reinstall", "--no-cache-dir"])
        args.extend(["-r", str(req)])
        good, detail = _run_pip(
            exe,
            args,
            log=log,
            label=f"Installing consumer requirements ({rel})",
            timeout=900,
        )
        if not good:
            ok = False
            if detail:
                details.append(detail)
    pyproject = root / "pyproject.toml"
    if not reqs and pyproject.is_file():
        args = ["install", "--upgrade"]
        if force:
            args.extend(["--force-reinstall", "--no-cache-dir"])
        args.append(str(root))
        good, detail = _run_pip(
            exe,
            args,
            log=log,
            label="Installing consumer package (pyproject)",
            timeout=900,
        )
        if not good:
            ok = False
            if detail:
                details.append(detail)
    return ok, "\n".join(details)


def _pip_install_bumps(
    exe: str,
    specs: list[str],
    *,
    force: bool = False,
    log=None,
) -> tuple[bool, str]:
    extras = ["pytest"]
    args = ["install", "--upgrade"]
    if force:
        args.extend(["--force-reinstall", "--no-cache-dir"])
    args.extend([*specs, *extras])
    kind = "force-reinstall" if force else "install"
    return _run_pip(
        exe,
        args,
        log=log,
        label=f"Installing bumped packages ({kind}): {' '.join(specs)}",
        timeout=300,
    )


def verify_env_healthy(exe: str, specs: list[str]) -> tuple[bool, str]:
    """True when bumped packages and pytest are importable / runnable."""
    modules = [_import_name(s) for s in specs]
    for mod in modules:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", mod):
            continue
        try:
            proc = subprocess.run(
                [exe, "-c", f"import {mod}"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"import {mod}: {exc}"
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = err[-1] if err else f"exit {proc.returncode}"
            return False, f"import {mod}: {detail}"
    try:
        proc = subprocess.run(
            [exe, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pytest: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = err[-1] if err else f"exit {proc.returncode}"
        return False, f"pytest: {detail}"
    return True, ""


@dataclass
class _SyncAttempt:
    req_ok: bool = True
    bump_ok: bool = True
    req_detail: str = ""
    bump_detail: str = ""


def _sync_once(
    exe: str,
    specs: list[str],
    *,
    root: Path | None,
    force: bool = False,
    upgrade_pip: bool = False,
    log=None,
) -> _SyncAttempt:
    if upgrade_pip:
        _upgrade_pip(exe, log=log)
    attempt = _SyncAttempt()
    if root is not None:
        attempt.req_ok, attempt.req_detail = _pip_install_consumer_reqs(
            exe, root, force=force, log=log
        )
        if not attempt.req_ok and is_dep_conflict(attempt.req_detail):
            # Conflicted requirements cannot be fixed by installing bumps alone.
            return attempt
    if specs:
        attempt.bump_ok, attempt.bump_detail = _pip_install_bumps(
            exe, specs, force=force, log=log
        )
    return attempt


def _abort_req_failure(detail: str, *, specs: list[str]) -> None:
    raise VerifyEnvError(format_verify_env_failure(detail, specs=specs))


def sync_bumped_packages(
    packet: dict[str, Any],
    *,
    python: str | None = None,
    root: Path | None = None,
    log=None,
) -> list[str]:
    """
    Install consumer requirements + DEPENDENCY_BUMP pins + pytest into the
    consumer verify interpreter.

    Order: requirements.txt (or pyproject) first, then bump pins so the
    migrated version wins. Recreates verify-venv on corruption. Raises
    ``VerifyEnvError`` when requirements cannot resolve (e.g. pin conflict).
    """
    specs = bumped_package_specs(packet)
    has_reqs = bool(root and consumer_req_files(root)) or bool(
        root and (root / "pyproject.toml").is_file()
    )
    if not specs and not has_reqs:
        return []
    if python is None and root is not None:
        from conduit.test_runner import ensure_consumer_python

        python = ensure_consumer_python(root, log=log)
    exe = python or sys.executable
    if root is not None:
        from conduit.test_runner import interpreter_belongs_to_root

        if not interpreter_belongs_to_root(exe, root):
            if log:
                log(
                    f"[yellow]Skipped pip install[/yellow] "
                    f"(interpreter {exe} is not inside {root})"
                )
            return specs

    attempt = _sync_once(exe, specs, root=root, force=False, log=log)
    if has_reqs and not attempt.req_ok and is_dep_conflict(attempt.req_detail):
        _abort_req_failure(attempt.req_detail, specs=specs)

    healthy = False
    reason = ""
    if attempt.req_ok and (attempt.bump_ok if specs else True):
        if specs:
            healthy, reason = verify_env_healthy(exe, specs)
        else:
            healthy, reason = True, ""
    elif not attempt.req_ok:
        reason = attempt.req_detail or "consumer requirements install failed"
    else:
        reason = attempt.bump_detail or "bumped package install failed"

    if healthy:
        return specs
    if not specs and attempt.req_ok:
        return specs

    if log:
        log(f"[yellow]Verify env unhealthy ({reason}); repairing verify-venv…[/yellow]")

    if root is not None:
        from conduit.test_runner import recreate_verify_venv

        exe = recreate_verify_venv(root, log=log) or exe

    attempt = _sync_once(
        exe, specs, root=root, force=True, upgrade_pip=True, log=log
    )
    if has_reqs and not attempt.req_ok:
        _abort_req_failure(attempt.req_detail, specs=specs)

    if attempt.req_ok and (attempt.bump_ok if specs else True):
        if specs:
            healthy, reason = verify_env_healthy(exe, specs)
        else:
            healthy, reason = True, ""
    else:
        healthy = False
        reason = attempt.bump_detail or attempt.req_detail or "pip reinstall failed"

    if healthy or (not specs and attempt.req_ok):
        if log:
            log("[green]Verify env repaired.[/green]")
        return specs

    if log:
        log(
            f"[yellow]Verify env still unhealthy after repair ({reason}). "
            "Continuing; tests may be skipped.[/yellow]"
        )
    return specs
