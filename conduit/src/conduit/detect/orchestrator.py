"""Core detect orchestrator: lockfile jumps + client state + vendor modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from conduit.detect.client_state import PackageClientState, scan_package_states
from conduit.detect.lockfile_diff import detect_lockfile_jumps
from conduit.detect.manifests import read_installed
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.base import DetectContext
from conduit.detect.modules.discovery import load_modules


@dataclass
class DetectResult:
    signals: list[ChangeSignal] = field(default_factory=list)
    installed: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    package_states: dict[str, PackageClientState] = field(default_factory=dict)

    @property
    def packages(self) -> set[str]:
        return {s.package for s in self.signals if s.package}


def run_detect(
    root: Path,
    *,
    base_ref: str | None = None,
    majors_only: bool = True,
    module_names: list[str] | None = None,
    skip_modules: bool = False,
    skip_lockfile: bool = False,
    demo: bool = False,
    verbose: bool = False,
    scan_client: bool = True,
    catalog_latest: bool = False,
    scan_packages: list[str] | None = None,
    log=None,
) -> DetectResult:
    root = root.resolve()
    installed = read_installed(root)
    signals: list[ChangeSignal] = []
    warnings: list[str] = []
    package_states: dict[str, PackageClientState] = {}
    scan_pkgs: list[str] = []
    modules = []

    if not skip_lockfile:
        for jump in detect_lockfile_jumps(
            root, base_ref=base_ref, majors_only=majors_only
        ):
            signals.append(jump.to_signal())
            installed.setdefault(jump.name, jump.to_version)

    if not skip_modules:
        modules = list(load_modules(names=module_names))
        for mod in modules:
            if (
                module_names is None
                and installed
                and not mod.applies(installed)
            ):
                continue
            scan_pkgs.extend(mod.packages or [mod.name])

    for extra in scan_packages or []:
        if extra and extra not in scan_pkgs:
            scan_pkgs.append(extra)

    if scan_client and scan_pkgs:
        package_states = scan_package_states(
            root,
            scan_pkgs,
            installed=installed,
            demo=demo,
            use_llm=not demo,
            log=log,
        )
        for state in package_states.values():
            for note in state.notes:
                if note.startswith("llm enrichment failed"):
                    warnings.append(f"client state {state.package}: {note}")

    if not skip_modules:
        ctx = DetectContext(
            repo_root=root,
            installed=installed,
            package_states=package_states,
            demo=demo,
            verbose=verbose,
            majors_only=majors_only,
            catalog_latest=catalog_latest,
        )
        for mod in modules:
            if (
                module_names is None
                and installed
                and not mod.applies(installed)
            ):
                continue
            try:
                signals.extend(mod.run(ctx))
            except Exception as exc:
                warnings.append(f"detect module {mod.name}: {exc}")
            warnings.extend(list(ctx.extra.get("warnings") or []))
            if verbose:
                warnings.extend(list(ctx.extra.get("verbose_warnings") or []))
            ctx.extra["warnings"] = []
            ctx.extra["verbose_warnings"] = []

    return DetectResult(
        signals=signals,
        installed=installed,
        warnings=warnings,
        package_states=package_states,
    )
