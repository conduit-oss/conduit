"""Compare public exports between two package versions to narrow migration scope."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from conduit.export_delta.diff import diff_exports
from conduit.export_delta.extract import extract_exports_from_tree
from conduit.export_delta.resolve import resolve_package_tree


@dataclass
class ExportDelta:
    package: str
    from_version: str
    to_version: str
    ecosystem: str
    added: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    renamed: dict[str, str] = field(default_factory=dict)
    from_symbols: set[str] = field(default_factory=set)
    to_symbols: set[str] = field(default_factory=set)
    from_tree: Path | None = None
    resource_paths: dict[str, str] = field(default_factory=dict)
    skipped_reason: str | None = None
    diagnostics: list[str] = field(default_factory=list)

    @property
    def changed_symbols(self) -> set[str]:
        return (
            set(self.removed)
            | set(self.added)
            | set(self.renamed.keys())
            | set(self.renamed.values())
        )

    @property
    def gone_symbols(self) -> set[str]:
        """Old names that died on the to-pin, including false-rename keys (Audio→audio)."""
        return set(self.removed) | set(self.renamed.keys())

    def to_dict(self) -> dict:
        return {
            "package": self.package,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "ecosystem": self.ecosystem,
            "added": sorted(self.added),
            "removed": sorted(self.removed),
            "renamed": dict(sorted(self.renamed.items())),
            "skipped_reason": self.skipped_reason,
            "diagnostics": list(self.diagnostics),
        }


def compute_export_delta(
    *,
    package: str,
    from_version: str,
    to_version: str,
    ecosystem: str = "pypi",
    cache_root: Path | None = None,
) -> ExportDelta:
    """Fetch both versions (cached) and diff public symbols. Soft-fails on errors."""
    result = ExportDelta(
        package=package,
        from_version=from_version,
        to_version=to_version,
        ecosystem=ecosystem,
    )
    if not package:
        result.skipped_reason = "missing package name"
        return result

    try:
        old_tree, old_err = resolve_package_tree(
            package, from_version, ecosystem=ecosystem, cache_root=cache_root
        )
        new_tree, new_err = resolve_package_tree(
            package, to_version, ecosystem=ecosystem, cache_root=cache_root
        )
    except Exception as exc:
        result.skipped_reason = f"resolve failed: {exc}"
        return result

    if old_err:
        result.diagnostics.append(f"from {package}=={from_version}: {old_err}")
    if new_err:
        result.diagnostics.append(f"to {package}=={to_version}: {new_err}")

    if old_tree is None or new_tree is None:
        failed = []
        if old_tree is None:
            failed.append(f"{package}=={from_version}")
        if new_tree is None:
            failed.append(f"{package}=={to_version}")
        result.skipped_reason = (
            "could not resolve " + " and ".join(failed) + f" ({ecosystem})"
        )
        return result

    try:
        old_syms = extract_exports_from_tree(old_tree, package=package, ecosystem=ecosystem)
        new_syms = extract_exports_from_tree(new_tree, package=package, ecosystem=ecosystem)
    except Exception as exc:
        result.skipped_reason = f"extract failed: {exc}"
        return result

    result.from_symbols = old_syms
    result.to_symbols = new_syms
    result.from_tree = old_tree
    added, removed, renamed = diff_exports(old_syms, new_syms)
    result.added = added
    result.removed = removed
    result.renamed = renamed
    try:
        from conduit.export_delta.resources import extract_resource_paths

        result.resource_paths = extract_resource_paths(old_tree, package=package)
    except Exception as exc:
        result.diagnostics.append(f"resource path extract failed: {exc}")
    return result


def prune_by_export_symbols(
    files: Iterable[Path],
    delta: ExportDelta,
) -> list[Path]:
    """
    Keep files that mention any changed symbol.
    If delta is unusable or no file mentions a symbol, return the original list.
    """
    file_list = list(files)
    symbols = {s for s in delta.changed_symbols if s and len(s) > 1}
    if delta.skipped_reason or not symbols:
        return file_list

    hits: list[Path] = []
    for path in file_list:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            hits.append(path)
            continue
        if any(sym in text for sym in symbols):
            hits.append(path)
    return hits if hits else file_list
