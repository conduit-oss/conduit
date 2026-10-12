"""Known external services for the outside-in inventory.

Python import roots and vendor hosts. This is the hero path (SDK clients
and raw REST), not a catalog of every PyPI library. ``spec_for_name`` is
the seam for an explicit module that is not listed here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    package: str
    modules: tuple[str, ...]
    hosts: tuple[str, ...] = ()
    #: ``exact`` matches the host or a subdomain. ``suffix`` matches a
    #: public suffix such as amazonaws.com.
    host_mode: str = "exact"
    constructor_tails: tuple[str, ...] = ()


CATALOG: dict[str, ServiceSpec] = {
    "openai": ServiceSpec(
        service_id="openai",
        package="openai",
        modules=("openai",),
        hosts=("api.openai.com",),
        constructor_tails=("OpenAI", "AzureOpenAI", "Client"),
    ),
    "anthropic": ServiceSpec(
        service_id="anthropic",
        package="anthropic",
        modules=("anthropic",),
        hosts=("api.anthropic.com",),
        constructor_tails=("Anthropic", "AsyncAnthropic"),
    ),
    "stripe": ServiceSpec(
        service_id="stripe",
        package="stripe",
        modules=("stripe",),
        hosts=("api.stripe.com",),
        constructor_tails=("StripeClient",),
    ),
    "twilio": ServiceSpec(
        service_id="twilio",
        package="twilio",
        modules=("twilio",),
        hosts=("api.twilio.com",),
        constructor_tails=("Client",),
    ),
    "boto3": ServiceSpec(
        service_id="boto3",
        package="boto3",
        modules=("boto3", "botocore"),
        hosts=("amazonaws.com",),
        host_mode="suffix",
        constructor_tails=("client", "resource", "Session"),
    ),
}

#: Roots whose call tails are HTTP, not vendor SDK methods.
HTTP_CLIENT_ROOTS = frozenset({"requests", "httpx", "urllib", "aiohttp", "httplib2"})
HTTP_METHOD_TAILS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "urlopen"}
)

LIMITATIONS: tuple[str, ...] = (
    "Python only. JS/TS should plug in beside analyze_python and emit the same map.",
    "Dynamic dispatch is invisible: getattr, globals(), a function stored on a parameter, and DI containers.",
    "Name binding inside a function is not flow-sensitive.",
    "Positional arguments are not matched to SDK parameter names.",
    "Star-imports are inventoried and not bound to call sites.",
    "Decorator calls and lambdas are not separate symbols.",
    "Directories named tests are skipped, same as the leftover scanner.",
)


def _lookup(name: str) -> ServiceSpec | None:
    key = (name or "").strip()
    if not key:
        return None
    if key in CATALOG:
        return CATALOG[key]
    low = key.lower().replace("-", "_")
    if low in CATALOG:
        return CATALOG[low]
    for spec in CATALOG.values():
        if key in spec.modules or key == spec.package or key == spec.service_id:
            return spec
    return None


def catalog_spec(name: str) -> ServiceSpec | None:
    """Catalogued external service, or None for ordinary libraries like pydantic."""
    return _lookup(name)


def spec_for_name(name: str) -> ServiceSpec:
    """Catalog hit, or a generic module spec for an explicit ``--service``."""
    found = _lookup(name)
    if found is not None:
        return found
    token = (name or "").strip()
    return ServiceSpec(service_id=token, package=token, modules=(token,))


def service_for_module(root: str, specs: tuple[ServiceSpec, ...]) -> ServiceSpec | None:
    for spec in specs:
        if root in spec.modules:
            return spec
    return None


def service_for_host(host: str, specs: tuple[ServiceSpec, ...]) -> ServiceSpec | None:
    name = (host or "").lower()
    if not name:
        return None
    exact = [spec for spec in specs if spec.host_mode != "suffix"]
    suffix = [spec for spec in specs if spec.host_mode == "suffix"]
    for group in (exact, suffix):
        for spec in group:
            for item in spec.hosts:
                if name == item or name.endswith("." + item):
                    return spec
    return None
