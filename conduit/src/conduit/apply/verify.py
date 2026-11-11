"""Re-check a patched file with Conduit impact (scratch tree)."""

from __future__ import annotations

from pathlib import Path

from conduit.apply.models import ImpactSite


def site_cleared_by_impact(
    root: Path,
    site: ImpactSite,
    patched: str,
    *,
    fixtures: Path | None = None,
    allow_network: bool = False,
) -> bool:
    """Return True when the call site is gone after re-running impact."""
    return not remaining_breaks(
        root, site, patched, fixtures=fixtures, allow_network=allow_network
    )


def remaining_breaks(
    root: Path,
    site: ImpactSite,
    patched: str,
    *,
    span: tuple[int, int] | None = None,
    fixtures: Path | None = None,
    allow_network: bool = False,
    original: str | None = None,
) -> list[tuple[str, str]]:
    """``(finding, symbol)`` pairs impact still reports for the original site or the rewritten span.

    Writes ``patched`` over ``site.path`` in place, then restores the previous
    text. Avoids copying the whole tree (large repos spent minutes per site on
    that alone). Calls ``collect_impacts`` for ``site.dependency`` with the same
    from/to versions. A hit counts when it shares the original path and
    expression (line may shift after the edit), or when it falls inside
    ``span`` (1-based, inclusive): a break the rewrite itself introduced,
    such as a parameter the new release removed.
    """
    from conduit.apply.repoindex import current_index

    target = root / site.path
    if original is None:
        try:
            original = target.read_text(encoding="utf-8")
        except OSError:
            original = ""
    previous: str | None
    try:
        previous = target.read_text(encoding="utf-8")
    except OSError:
        previous = None
    target.parent.mkdir(parents=True, exist_ok=True)
    index = current_index()
    if index is not None:
        # Recheck sees the patched file through the index; other modules stay as
        # indexed (no whole-repo re-parse). Restore both disk and index after.
        index.update(site.path, patched)
    try:
        target.write_text(patched, encoding="utf-8")
        return _breaks_listed(
            root,
            site,
            span=span,
            fixtures=fixtures,
            allow_network=allow_network,
            original=original or "",
        )
    finally:
        if previous is None:
            try:
                target.unlink()
            except OSError:
                pass
            if index is not None:
                index.update(site.path, "")
        else:
            target.write_text(previous, encoding="utf-8")
            if index is not None:
                index.update(site.path, previous)


def _breaks_listed(
    root: Path,
    site: ImpactSite,
    *,
    span: tuple[int, int] | None,
    fixtures: Path | None,
    allow_network: bool,
    original: str = "",
) -> list[tuple[str, str]]:
    from conduit.impact.run import collect_impacts
    from conduit.outside_in.bridge import build_service_map

    from conduit.outside_in.library_types import installed_version, types_from

    dep, pinned = site.dependency or "", site.from_version or ""
    typed = {dep: pinned} if dep and pinned and installed_version(dep) not in {"", pinned} else {}
    # Recheck only needs files that import this dependency (plus the patched
    # file). Whole-repo maps dominate apply time on mid/large trees.
    only = _recheck_files(root, dep, site.path)
    with types_from(typed):
        service_map, _blast = build_service_map(
            root, service=dep or None, only_files=only
        )
    impacts, _diffed, _notes = collect_impacts(
        root,
        service_map,
        fixtures=fixtures,
        dep=site.dependency or None,
        to_version=site.to_version or None,
        from_version=site.from_version or None,
        allow_network=allow_network,
    )
    want_path = site.path.replace("\\", "/")
    want_expr = (site.expr or "").strip()
    found: list[tuple[str, str]] = []
    for item in impacts:
        if item.kind == "deprecated":
            continue
        for hit in item.sites:
            if hit.via_wrapper:
                continue
            hit_path = hit.path.replace("\\", "/")
            if hit_path != want_path:
                continue
            same = (hit.expr == want_expr) if want_expr else hit.line == site.line
            inside = span is not None and span[0] <= hit.line <= span[1]
            if same or inside:
                text = (f"{hit_path}:{hit.line} {hit.expr}: {item.change}", item.symbol)
                if text not in found:
                    found.append(text)
    if span is not None:
        _merge_keyword_findings(
            found, _unknown_keyword_breaks(service_map, site, want_path, span, fixtures, allow_network)
        )
        # Declared/dict-key/type checks need the whole repo (callee defs and
        # example calls), not only SDK importers. Map analysis stays scoped.
        for item in _type_breaks(
            root, service_map, site, want_path, span, fixtures, allow_network, original
        ):
            if item not in found:
                found.append(item)
    return found


def _recheck_files(root: Path, dependency: str, site_path: str) -> list[str] | None:
    """Relative paths to analyze for a single-site recheck, or ``None`` for the whole tree.

    When the dependency name is known, only files that appear to import it (plus
    the patched file) are walked. That keeps apply rechecks off the whole-repo
    map path that dominated wall time on large trees.
    """
    dep = (dependency or "").strip()
    if not dep:
        return None
    from conduit.prune.grep_imports import prune_by_imports

    hits = prune_by_imports(root, [dep.split(".")[0] or dep])
    rels: list[str] = []
    seen: set[str] = set()
    want = site_path.replace("\\", "/")
    for path in hits:
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if rel not in seen:
            seen.add(rel)
            rels.append(rel)
    if want and want not in seen:
        rels.append(want)
    return rels or None


def _merge_keyword_findings(found: list[tuple[str, str]], extra: list[tuple[str, str]]) -> None:
    """Add unknown-keyword findings unless the same name on the same line is already a removed parameter."""
    for text, symbol in extra:
        name = text.split(": keyword ", 1)[-1].split(" ", 1)[0]
        line = text.split(" ", 1)[0]
        if any(
            existing.split(" ", 1)[0] == line and f"parameter {name} " in existing
            for existing, _symbol in found
        ):
            continue
        if (text, symbol) not in found:
            found.append((text, symbol))


def _type_breaks(
    root: Path, service_map, site: ImpactSite, path: str, span: tuple[int, int], fixtures, allow_network,
    original: str = "",
) -> list[tuple[str, str]]:
    """Argument kinds and attribute reads in the span that the new release's types rule out."""
    import ast

    from conduit.apply.signatures import _modules, new_release_wheel
    from conduit.apply.typecheck import (
        argument_breaks,
        attribute_breaks,
        dropped_arguments,
        original_arguments,
        repo_call_breaks,
    )

    try:
        source = (root / path).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    try:
        # Calls into this repo's own classes and functions, against their own signatures.
        local = [(text, "") for text in repo_call_breaks(root, path, source, span)]
    except Exception:  # noqa: BLE001 — a static check that cannot run stays silent
        local = []
    wheel = new_release_wheel(
        site.dependency, site.to_version, fixtures=fixtures, allow_network=allow_network
    )
    _record_drops(site, original, tree, span, service_map, wheel, fixtures, allow_network)
    local.extend(_declared_breaks(root, service_map, path, source, span, original, wheel))
    if wheel is None:
        return local
    try:
        modules = _modules(str(wheel))
        found: list[tuple[str, str]] = list(local)
        old_wheel = new_release_wheel(
            site.dependency, site.from_version, fixtures=fixtures, allow_network=allow_network
        )
        passed_before = original_arguments(
            original, site, _modules(str(old_wheel)) if old_wheel is not None else {}
        )
        for chain in service_map.chains:
            if (
                chain.call_rel.replace("\\", "/") == path
                and span[0] <= chain.call_lineno <= span[1]
                and not chain.via_wrapper
                and chain.kind.endswith("_call")
            ):
                for text in argument_breaks(
                    modules, tree, path, chain, site.dependency or "", site.to_version or ""
                ):
                    found.append((text, chain.sdk_method))
                for text in dropped_arguments(
                    modules, tree, path, chain, passed_before, site.dependency or "", site.to_version or ""
                ):
                    found.append((text, chain.sdk_method))
        for text in attribute_breaks(
            modules, root, path, source, span, service_map.chains,
            site.dependency or "", site.to_version or "",
        ):
            found.append((text, ""))
        return found
    except Exception:  # noqa: BLE001 — a static check that cannot run stays silent
        return local


def _declared_breaks(root: Path, service_map, path: str, source: str, span, original: str, wheel) -> list[tuple[str, str]]:
    """Values whose declared class is not the class the callee declares (see ``declared``)."""
    import ast

    from conduit.apply.declared import declared_type_breaks
    from conduit.apply.signatures import _modules, resolve_callable

    try:
        modules = _modules(str(wheel)) if wheel is not None else {}
        traced: dict[tuple[int, str], tuple[str, ast.AST]] = {}
        for chain in service_map.chains:
            if (
                modules
                and chain.call_rel.replace("\\", "/") == path
                and span[0] <= chain.call_lineno <= span[1]
                and not chain.via_wrapper
                and chain.kind.endswith("_call")
            ):
                found = resolve_callable(modules, chain.sdk_method)
                if found is not None:
                    traced[(chain.call_lineno, chain.call_expr)] = found
        return [(text, "") for text in declared_type_breaks(modules, root, path, source, span, original, traced)]
    except Exception:  # noqa: BLE001 — a static check that cannot run stays silent
        return []


def _record_drops(site, original: str, tree, span, service_map, wheel, fixtures, allow_network) -> None:
    """Note, for the per-site check only, the arguments the rewrite stopped passing."""
    import ast

    from conduit.apply.dropped import DROPPED, dropped_from, note_for
    from conduit.apply.signatures import _modules, new_release_wheel, resolve_callable, unknown_keywords
    from conduit.apply.typecheck import _call_at

    sink = DROPPED.get()
    if sink is None:
        return
    try:
        old = _call_at(ast.parse(original), site.line, (site.expr or "").strip())
        if old is None:
            return
        order: list[str] = []
        old_wheel = new_release_wheel(
            site.dependency, site.from_version, fixtures=fixtures, allow_network=allow_network
        )
        found = resolve_callable(_modules(str(old_wheel)), site.symbol) if old_wheel is not None and site.symbol else None
        if found is not None:
            order = [arg.arg for arg in (*found[1].args.posonlyargs, *found[1].args.args)]
            order = order[1:] if order and order[0] in {"self", "cls"} else order
        names, new = dropped_from(old, tree, span, order)
        if not names or new is None:
            return
        owner, rejected = "", []
        for chain in service_map.chains:
            if chain.call_lineno == new.lineno and chain.call_expr == ast.unparse(new.func) and not chain.via_wrapper:
                owner, rejected = unknown_keywords(wheel, chain.sdk_method, tuple(names))
                break
        sink["names"] = names
        sink["note"] = note_for(names, rejected, owner)
    except Exception:  # noqa: BLE001 — a note that cannot be built is left out
        return


def _unknown_keyword_breaks(
    service_map, site: ImpactSite, path: str, span: tuple[int, int], fixtures, allow_network
) -> list[tuple[str, str]]:
    """Keywords a call in the rewritten span passes that the new release's callable rejects.

    Covers callables that are new in that release (no diff entry to compare).
    """
    from conduit.apply.signatures import new_release_wheel, unknown_keywords

    calls = [
        chain
        for chain in service_map.chains
        if chain.call_rel.replace("\\", "/") == path
        and span[0] <= chain.call_lineno <= span[1]
        and not chain.via_wrapper
        and chain.kind.endswith("_call")
        and chain.site_keywords
    ]
    if not calls:
        return []
    wheel = new_release_wheel(
        site.dependency, site.to_version, fixtures=fixtures, allow_network=allow_network
    )
    found: list[tuple[str, str]] = []
    for chain in calls:
        owner, rejected = unknown_keywords(wheel, chain.sdk_method, chain.site_keywords)
        for name in rejected:
            text = (
                f"{path}:{chain.call_lineno} {chain.call_expr}: keyword {name} is not a "
                f"parameter of {owner} in {site.dependency} {site.to_version}",
                chain.sdk_method,
            )
            if text not in found:
                found.append(text)
    return found
