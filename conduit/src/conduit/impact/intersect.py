"""Keep only the API changes that touch a call site in this repo."""

from __future__ import annotations

from conduit.impact.service_model import operation_matches
from conduit.impact.model import ApiChange, Impact, SiteHit
from conduit.outside_in.graph import min_confidence
from conduit.outside_in.model import Chain


def intersect(
    changes: list[ApiChange],
    chains: tuple[Chain, ...] | list[Chain],
    *,
    dependency: str,
    from_version: str,
    to_version: str,
    return_classes: dict[str, set[str]] | None = None,
    surviving: set[str] | None = None,
    methods: dict[str, str] | None = None,
) -> list[Impact]:
    relevant = [chain for chain in chains if _same_dependency(chain, dependency)]
    impacts: list[Impact] = []
    covered: list[tuple[str, str]] = []
    # Two removed symbols can share one detail and one call. Emit that
    # file:line once per change text. A different change on the same line stays.
    emitted: set[tuple] = set()
    ordered = sorted(changes, key=lambda item: (0 if item.confidence != "low" else 1, item.detail))
    for change in ordered:
        if change.confidence == "low" and any(change.old and change.old == old for old, _site in covered):
            continue
        sites = _sites(
            change,
            relevant,
            changes,
            return_classes or {},
            surviving or set(),
            methods or {},
        )
        fresh: list[SiteHit] = []
        # One removed name can appear under its defining path and a public
        # re-export (``types.completion.Completion`` and ``types.Completion``).
        # The parameter (``old``) stays in the key: two removed keywords on one
        # call are two breaks.
        short = (
            change.kind,
            (change.symbol or "").rsplit(".", 1)[-1],
            (change.old or "").rsplit(".", 1)[-1],
        )
        for site in sites:
            key = (site.path, site.line, change.detail)
            same = (site.path, site.line, site.expr, *short)
            if key in emitted or same in emitted:
                continue
            emitted.add(key)
            emitted.add(same)
            fresh.append(site)
        sites = fresh
        if not sites:
            continue
        for site in sites:
            if change.old:
                covered.append((change.old, f"{site.path}:{site.line}"))
        impacts.append(
            Impact(
                dependency=dependency,
                from_version=from_version,
                to_version=to_version,
                change=change.detail,
                confidence=_impact_confidence(change, sites),
                source=change.source,
                sites=tuple(sites),
                kind=change.kind,
                symbol=change.symbol,
                old=change.old,
                new=change.new,
            )
        )
    return impacts


def _sites(
    change: ApiChange,
    chains: list[Chain],
    peers: list[ApiChange],
    return_classes: dict[str, set[str]],
    surviving: set[str],
    methods: dict[str, str] | None = None,
) -> list[SiteHit]:
    found: list[SiteHit] = []
    seen: set[tuple[str, int, str]] = set()
    for chain in chains:
        confidence = _match(change, chain, peers, return_classes, surviving, methods or {})
        if confidence is None:
            continue
        origins = _param_origin_hits(change, chain, confidence)
        if origins:
            for site in origins:
                key = (site.path, site.line, site.expr)
                if key in seen:
                    continue
                seen.add(key)
                found.append(site)
            continue
        key = (chain.call_rel, chain.call_lineno, chain.call_expr)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            SiteHit(
                path=chain.call_rel,
                line=chain.call_lineno,
                expr=chain.call_expr,
                confidence=confidence,
                via_wrapper=chain.via_wrapper,
            )
        )
    return found


def _param_origin_hits(
    change: ApiChange, chain: Chain, confidence: str
) -> list[SiteHit]:
    """Primary lines where a removed/renamed keyword was introduced into a splat.

    The SDK call stays in the expression as context. Direct keywords on the call
    have no separate origin, so this returns empty and the call line is flagged.
    """
    if change.kind not in {"param_removed", "param_renamed", "param_added"}:
        return []
    if chain.via_wrapper:
        return []
    name = (change.old or change.new or "").lower()
    if not name or not chain.param_origins:
        return []
    call = f"{chain.call_rel}:{chain.call_lineno}"
    if chain.call_expr:
        call = f"{call} {chain.call_expr}"
    found: list[SiteHit] = []
    seen: set[tuple[str, int]] = set()
    for param, path, line, expr in chain.param_origins:
        if param.lower() != name:
            continue
        key = (path, line)
        if key in seen:
            continue
        seen.add(key)
        context = f"{expr} → {call}" if expr else call
        found.append(
            SiteHit(
                path=path,
                line=line,
                expr=context,
                confidence=confidence,
                via_wrapper=False,
            )
        )
    return found


def _match(
    change: ApiChange,
    chain: Chain,
    peers: list[ApiChange],
    return_classes: dict[str, set[str]],
    surviving: set[str],
    methods: dict[str, str] | None = None,
) -> str | None:
    if change.source == "webhook":
        return _webhook_match(change, chain)
    if change.source == "service-model":
        return _service_model_match(change, chain)
    if change.source == "openapi" or change.kind in {"endpoint_removed", "endpoint_renamed"}:
        return _http_match(change, chain)
    if chain.kind in {"version_gate", "http_call", "http_response"}:
        return None
    return _python_match(change, chain, peers, return_classes, surviving, methods)


def _http_match(change: ApiChange, chain: Chain) -> str | None:
    from conduit.impact.openapi_rest import paths_match, split_http_target

    if chain.kind not in {"http_call", "http_response"}:
        return None
    method, path = split_http_target(chain.sdk_method)
    if " " in (change.symbol or ""):
        spec_method, spec_path = change.symbol.split(" ", 1)
    else:
        spec_method, spec_path = "", change.old or change.symbol
    if spec_method in {"", "*"}:
        spec_method = method
    if method and spec_method and method.upper() != spec_method.upper():
        return None
    if not path or not (paths_match(path, spec_path) or paths_match(path, change.old)):
        return None
    if change.kind in {"endpoint_removed", "endpoint_renamed"}:
        if chain.kind != "http_call":
            return None
        return min_confidence(change.confidence, chain.confidence)
    if change.kind == "param_added" and "became required" in (change.detail or ""):
        # A newly required request field: flag the call when the site omits it.
        if chain.kind != "http_call":
            return None
        name = change.new or change.old
        if not name or _param_used(chain, name):
            return None
        return min_confidence(change.confidence, chain.confidence)
    if change.kind not in {"field_removed", "field_renamed", "param_removed", "param_renamed"}:
        return None
    name = change.old or change.new
    response = "response field" in (change.detail or "")
    if response:
        if chain.kind != "http_response" or not _field_used(chain, name):
            return None
    elif chain.kind != "http_call" or not _param_used(chain, name):
        return None
    return min_confidence(change.confidence, chain.confidence)


def _webhook_match(change: ApiChange, chain: Chain) -> str | None:
    if chain.kind != "webhook":
        return None
    fields = [item for item in (*chain.response_fields, *chain.params) if item]
    if not fields:
        return None
    for field in fields:
        if field == change.symbol or field == change.old:
            return min_confidence(change.confidence, chain.confidence)
        if change.symbol and (field.endswith("." + change.symbol) or change.symbol.endswith("." + field)):
            return min_confidence(min_confidence(change.confidence, "medium"), chain.confidence)
    return None


def _service_model_match(change: ApiChange, chain: Chain) -> str | None:
    if chain.kind not in {"cloud_call", "cloud_constructor"}:
        return None
    operation = change.symbol.split(".", 1)[-1]
    if not operation_matches(chain.sdk_method, change.service, operation):
        return None
    if change.kind in {"removed", "renamed"}:
        return min_confidence(change.confidence, chain.confidence)
    if change.kind in {"param_removed", "param_renamed", "param_added"}:
        if not _param_used(chain, change.old or change.new):
            return None
        return min_confidence(change.confidence, chain.confidence)
    if change.kind == "field_removed":
        if not _field_used(chain, change.old):
            return None
        return min_confidence(change.confidence, chain.confidence)
    return None


def _python_match(
    change: ApiChange,
    chain: Chain,
    peers: list[ApiChange],
    return_classes: dict[str, set[str]] | None = None,
    surviving: set[str] | None = None,
    methods: dict[str, str] | None = None,
) -> str | None:
    if chain.kind == "webhook":
        return None
    if change.kind == "response_shape":
        if not chain.kind.endswith("_response"):
            return None
        how = _method_match(
            chain.sdk_method,
            change.symbol,
            change,
            peers,
            return_classes or {},
            surviving or set(),
            methods or {},
        )
        if how is None:
            return None
        base = change.confidence if how == "exact" else min_confidence(change.confidence, "medium")
        return min_confidence(base, chain.confidence)
    if chain.kind.endswith("_response"):
        return None
    if chain.kind.endswith("_alias") and change.kind not in {"removed", "renamed"}:
        # An import line only breaks when the imported name is gone.
        return None
    how = _method_match(
        chain.sdk_method, change.symbol, change, peers, return_classes, surviving, methods
    )
    if how is None and change.kind == "param_renamed" and "." not in change.symbol:
        if _param_used(chain, change.old):
            return min_confidence(change.confidence, chain.confidence)
        return None
    if how is None:
        return None
    base = change.confidence if how == "exact" else min_confidence(change.confidence, "medium")
    if change.kind in {"param_removed", "param_renamed", "param_added"}:
        name = change.old or change.new
        if name and not _param_used(chain, name):
            return None
    return min_confidence(base, chain.confidence)


def _method_match(
    sdk_method: str,
    symbol: str,
    change: ApiChange,
    peers: list[ApiChange],
    return_classes: dict[str, set[str]] | None = None,
    surviving: set[str] | None = None,
    methods: dict[str, str] | None = None,
) -> str | None:
    """Match a call to a diff symbol.

    Exact means the call's resolved name is the defining path or a public
    re-export, including after the wheel's return class is substituted
    (``client.messages.create`` is ``Messages.create``). Otherwise the last
    two parts (``ChatCompletion.create``) match when that pair is unique.
    The bare last part (``create``) is never enough: too many methods share
    it. A call whose name the new wheel still exports is not that pair.
    """
    if not sdk_method or not symbol or sdk_method == "webhook":
        return None
    candidates = _with_return_classes(sdk_method, return_classes or {}, methods or {})
    for candidate in candidates:
        for name in (symbol, *change.aliases):
            if name and (candidate == name or candidate.endswith("." + name)):
                return "exact"
    if _still_exported(sdk_method, surviving or set()):
        return None
    pair = _last_two(symbol)
    if not pair:
        return None
    if not any(item == pair or item.endswith("." + pair) for item in candidates):
        return None
    same = [
        item
        for item in peers
        if item.kind == change.kind and item.source == change.source and _last_two(item.symbol) == pair
    ]
    if len(same) == 1:
        return "pair"
    return None


def _with_return_classes(
    sdk_method: str,
    return_classes: dict[str, set[str]],
    methods: dict[str, str] | None = None,
) -> set[str]:
    """Rewrite ``Factory.method`` once the wheel says what ``Factory`` returns.

    ``__getitem__`` is the subscript ``client[name]``. Several rewrites fold
    ``Owner.__getitem__.__getitem__.insert`` into the class that method returns.
    A method the return class inherits is rewritten to the class that defines it.
    """
    owners = methods or {}
    found = {sdk_method}
    for _ in range(6):
        extra: set[str] = set()
        for item in found:
            defined = owners.get(item)
            if defined and defined not in found:
                extra.add(defined)
            parts = item.split(".")
            for index in range(len(parts) - 1, 0, -1):
                prefix = ".".join(parts[:index])
                classes = return_classes.get(prefix)
                if not classes:
                    continue
                rest = ".".join(parts[index:])
                head, _, tail = rest.partition(".")
                for klass in classes:
                    extra.add(f"{klass}.{rest}")
                    defined = owners.get(f"{klass}.{head}")
                    if defined:
                        extra.add(defined if not tail else f"{defined}.{tail}")
                break
        if not extra - found:
            break
        found |= extra
    return found


def _still_exported(sdk_method: str, surviving: set[str]) -> bool:
    """True when the call's name is still a public symbol in the new wheel.

    A surviving name matches when it is the call or a dotted suffix of it
    (``engine.Engine`` still exported, so ``pkg.engine.Engine`` is not
    a removal). Suffixes are looked up in the set rather than scanning it.
    """
    if not sdk_method or not surviving:
        return False
    if sdk_method in surviving:
        return True
    parts = sdk_method.split(".")
    for index in range(1, len(parts)):
        if ".".join(parts[index:]) in surviving:
            return True
    return False


def _last_two(qualname: str) -> str:
    parts = [part for part in (qualname or "").split(".") if part]
    if len(parts) < 2:
        return ""
    return f"{parts[-2]}.{parts[-1]}"


def _param_used(chain: Chain, name: str) -> bool:
    if not name:
        return False
    wanted = name.lower()
    for param in (*chain.params, *chain.site_keywords):
        if param.lower() == wanted:
            return True
    return False


def _field_used(chain: Chain, name: str) -> bool:
    if not name:
        return False
    wanted = name.lower()
    for field in chain.response_fields:
        token = field.strip("[]'\"")
        if token.lower() == wanted or token.lower().endswith("." + wanted) or wanted in token.lower():
            return True
    return False


def _same_dependency(chain: Chain, dependency: str) -> bool:
    key = (dependency or "").lower()
    return chain.package.lower() == key or chain.service.lower() == key


def _impact_confidence(change: ApiChange, sites: list[SiteHit]) -> str:
    confidence = change.confidence
    for site in sites:
        confidence = min_confidence(confidence, site.confidence)
    return confidence
