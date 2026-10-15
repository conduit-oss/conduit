"""Keep only the API changes that touch a call site in this repo."""

from __future__ import annotations

from conduit.impact.botocore import operation_matches
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
) -> list[Impact]:
    relevant = [chain for chain in chains if _same_dependency(chain, dependency)]
    impacts: list[Impact] = []
    covered: list[tuple[str, str]] = []
    ordered = sorted(changes, key=lambda item: (0 if item.confidence != "low" else 1, item.detail))
    for change in ordered:
        if change.confidence == "low" and any(change.old and change.old == old for old, _site in covered):
            continue
        sites = _sites(change, relevant, changes)
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
            )
        )
    return impacts


def _sites(
    change: ApiChange,
    chains: list[Chain],
    peers: list[ApiChange],
) -> list[SiteHit]:
    found: list[SiteHit] = []
    seen: set[tuple[str, int, str]] = set()
    for chain in chains:
        confidence = _match(change, chain, peers)
        if confidence is None:
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


def _match(change: ApiChange, chain: Chain, peers: list[ApiChange]) -> str | None:
    if change.source == "webhook":
        return _webhook_match(change, chain)
    if change.source == "botocore":
        return _botocore_match(change, chain)
    if change.source == "openapi" or change.kind in {"endpoint_removed", "endpoint_renamed"}:
        return _http_match(change, chain)
    if chain.kind in {"version_gate", "http_call", "http_response"}:
        return None
    return _python_match(change, chain, peers)


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


def _botocore_match(change: ApiChange, chain: Chain) -> str | None:
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


def _python_match(change: ApiChange, chain: Chain, peers: list[ApiChange]) -> str | None:
    if chain.kind == "webhook":
        return None
    if change.kind == "response_shape":
        if not chain.kind.endswith("_response"):
            return None
        how = _method_match(chain.sdk_method, change.symbol, change, peers)
        if how is None:
            return None
        base = change.confidence if how == "exact" else min_confidence(change.confidence, "medium")
        return min_confidence(base, chain.confidence)
    if chain.kind.endswith("_response"):
        return None
    how = _method_match(chain.sdk_method, change.symbol, change, peers)
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


def _method_match(sdk_method: str, symbol: str, change: ApiChange, peers: list[ApiChange]) -> str | None:
    """Match a call to a diff symbol.

    Exact means the call's resolved name is the defining path or a public
    re-export. Otherwise the last two parts (``ChatCompletion.create``) match
    when that pair is unique. The bare last part (``create``) is never enough:
    too many methods share it.
    """
    if not sdk_method or not symbol or sdk_method == "webhook":
        return None
    for name in (symbol, *change.aliases):
        if name and (sdk_method == name or sdk_method.endswith("." + name)):
            return "exact"
    pair = _last_two(symbol)
    if not pair:
        return None
    if sdk_method != pair and not sdk_method.endswith("." + pair):
        return None
    same = [
        item
        for item in peers
        if item.kind == change.kind and item.source == change.source and _last_two(item.symbol) == pair
    ]
    if len(same) == 1:
        return "pair"
    return None


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
