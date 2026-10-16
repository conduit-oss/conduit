"""Outside services the code talks to.

A service spec is derived from a distribution's own code: network I/O,
a DB-API 2.0 connection, or a versioned API model. This module does not
list product names, hosts, or constructors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_DIST_NORMALIZE = re.compile(r"[-_.]+")


def normalize_dist(name: str) -> str:
    """PEP 503 name: lowercase, runs of ``-``, ``_``, and ``.`` become one ``-``."""
    return _DIST_NORMALIZE.sub("-", (name or "").strip().lower())


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    package: str
    modules: tuple[str, ...]
    hosts: tuple[str, ...] = ()
    #: ``exact`` matches the host or a subdomain. ``suffix`` matches a
    #: public suffix the distribution itself declares.
    host_mode: str = "exact"
    constructor_tails: tuple[str, ...] = ()
    #: ``api`` (external API client), ``database``, or ``cloud``.
    kind: str = "api"
    #: Import prefixes kept for callers that still pass a family spec.
    module_prefixes: tuple[str, ...] = ()
    #: Empty. Family sentinels are not synthesized from package-name prefixes.
    family: str = ""
    evidence: str = ""
    #: Generic HTTP or socket library. Not an outside service on the map.
    transport: bool = False


HTTP_METHOD_TAILS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "urlopen"}
)

LIMITATIONS: tuple[str, ...] = (
    "Python only. JS/TS should plug in beside analyze_python and emit the same map.",
    "Not a Dependabot replacement: no lockfile bumps, no CVE alerts, and no new-version notices for ordinary libraries.",
    "A variable passed to getattr, globals(), a callable stored on a parameter, and DI containers are invisible. A string literal passed to getattr, functools.partial, and a lambda that calls the SDK are tracked.",
    "Imports inside a function are tracked, including an import in an enclosing function. Name binding inside a function is not flow-sensitive past that.",
    "Positional arguments are not matched to SDK parameter names.",
    "A cloud service chosen by a variable (client(name)) is not tied to an operation.",
    "Webhook payload fields are the ones read in the handler and a helper it calls in the same file.",
    "Star-imports are inventoried and not bound to call sites.",
    "Decorator calls are not separate symbols. A lambda assigned to a name is.",
    "Directories named tests are skipped, same as the leftover scanner.",
    "REST calls are mapped by host and path. A versioned OpenAPI pair flags removed paths and fields; without that pair the call is reported as not compared.",
)


def catalog_spec(
    name: str,
    version: str | None = None,
    *,
    root: Path | None = None,
) -> ServiceSpec | None:
    """Product client for this distribution, or None for ordinary libraries and HTTP transports."""
    from conduit.outside_in.evidence import spec_for_distribution

    return spec_for_distribution(name, version, root=root)


def spec_for_name(name: str) -> ServiceSpec:
    """Classifier hit, or a generic module spec for an explicit ``--service``."""
    found = catalog_spec(name)
    if found is not None:
        return found
    token = (name or "").strip()
    return ServiceSpec(service_id=token, package=token, modules=(token,))


def service_for_module(module: str, specs: tuple[ServiceSpec, ...]) -> ServiceSpec | None:
    """Longest module or prefix match."""
    target = (module or "").strip()
    if not target:
        return None
    best: ServiceSpec | None = None
    best_len = -1
    for spec in specs:
        for name in (*spec.modules, *spec.module_prefixes):
            if target == name or target.startswith(name + "."):
                if len(name) > best_len:
                    best = spec
                    best_len = len(name)
    return best


def service_for_host(host: str, specs: tuple[ServiceSpec, ...]) -> ServiceSpec | None:
    name = (host or "").lower()
    if not name:
        return None
    exact = [spec for spec in specs if spec.host_mode != "suffix" and not spec.transport]
    suffix = [spec for spec in specs if spec.host_mode == "suffix" and not spec.transport]
    for group in (exact, suffix):
        for spec in group:
            for item in spec.hosts:
                if name == item or name.endswith("." + item):
                    return spec
    return None
