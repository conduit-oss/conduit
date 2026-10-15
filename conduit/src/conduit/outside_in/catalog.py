"""Outside services the code talks to.

API SDKs and REST hosts, database drivers and ORMs, and cloud SDKs.
This is not a catalog of every PyPI library. Ordinary packages (pydantic,
requests as a library, your own modules) stay out. ``spec_for_name`` is
the seam for an explicit module that is not listed here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


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
    #: ``sdk`` (external API client), ``database``, or ``cloud``.
    kind: str = "sdk"
    #: Import prefixes such as ``google.cloud`` or ``azure``.
    module_prefixes: tuple[str, ...] = ()
    #: ``google-cloud`` or ``azure``: the PyPI name comes from the import path.
    family: str = ""


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
        kind="cloud",
    ),
    "sqlalchemy": ServiceSpec(
        service_id="sqlalchemy",
        package="sqlalchemy",
        modules=("sqlalchemy",),
        kind="database",
        constructor_tails=("create_engine", "sessionmaker", "Session"),
    ),
    "psycopg": ServiceSpec(
        service_id="psycopg",
        package="psycopg",
        modules=("psycopg",),
        kind="database",
        constructor_tails=("connect",),
    ),
    "psycopg2": ServiceSpec(
        service_id="psycopg2",
        package="psycopg2",
        modules=("psycopg2",),
        kind="database",
        constructor_tails=("connect",),
    ),
    "pymongo": ServiceSpec(
        service_id="pymongo",
        package="pymongo",
        modules=("pymongo",),
        kind="database",
        constructor_tails=("MongoClient",),
    ),
    "redis": ServiceSpec(
        service_id="redis",
        package="redis",
        modules=("redis",),
        kind="database",
        constructor_tails=("Redis", "StrictRedis"),
    ),
    "asyncpg": ServiceSpec(
        service_id="asyncpg",
        package="asyncpg",
        modules=("asyncpg",),
        kind="database",
        constructor_tails=("connect",),
    ),
    "acme": ServiceSpec(
        service_id="acme",
        package="acme-sdk",
        modules=("acme_sdk",),
        hosts=("api.acme.example",),
        constructor_tails=("Client",),
    ),
    "github": ServiceSpec(
        service_id="github",
        package="github",
        modules=(),
        hosts=("api.github.com", "github.com"),
    ),
    "airflow": ServiceSpec(
        service_id="airflow",
        package="apache-airflow-client",
        modules=("airflow_client",),
        # Generated clients use relative OpenAPI paths; host is only for URL shaping.
        hosts=("airflow.apache.org",),
        constructor_tails=("ApiClient", "Configuration"),
    ),
    "clerk": ServiceSpec(
        service_id="clerk",
        package="clerk-backend-api",
        modules=("clerk_backend_api", "clerk_client"),
        hosts=("api.clerk.com",),
        constructor_tails=("Clerk", "ApiClient"),
    ),
    "google-cloud": ServiceSpec(
        service_id="google-cloud",
        package="google-cloud",
        modules=(),
        module_prefixes=("google.cloud",),
        kind="cloud",
        family="google-cloud",
    ),
    "azure": ServiceSpec(
        service_id="azure",
        package="azure",
        modules=(),
        module_prefixes=("azure",),
        kind="cloud",
        family="azure",
    ),
}

#: Roots whose call tails are HTTP, not vendor SDK methods.
HTTP_CLIENT_ROOTS = frozenset({"requests", "httpx", "urllib", "aiohttp", "httplib2"})
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
    """Catalogued outside service, or None for ordinary libraries like pydantic.

    ``google-cloud-*`` and ``azure-*`` package names count. A family sentinel
    (``google-cloud``, ``azure``) counts. Anything else does not.
    """
    found = _lookup(name)
    if found is not None and not found.family:
        return found
    synthesized = synthesize_package(name)
    if synthesized is not None:
        return synthesized
    if found is not None:
        return found
    return None


def spec_for_name(name: str) -> ServiceSpec:
    """Catalog hit, or a generic module spec for an explicit ``--service``."""
    found = catalog_spec(name)
    if found is not None:
        return found
    token = (name or "").strip()
    return ServiceSpec(service_id=token, package=token, modules=(token,))


def synthesize_package(name: str) -> ServiceSpec | None:
    """PyPI name for one google-cloud or azure product, from the package name."""
    token = (name or "").strip().lower()
    if token.startswith("google-cloud-") and len(token) > len("google-cloud-"):
        product = token[len("google-cloud-") :]
        module = "google.cloud." + product.replace("-", "_")
        return ServiceSpec(
            service_id=token,
            package=token,
            modules=(module,),
            kind="cloud",
        )
    if token.startswith("azure-") and len(token) > len("azure-"):
        parts = [part for part in token.split("-")[1:] if part]
        if not parts:
            return None
        module = "azure." + ".".join(part.replace("-", "_") for part in parts)
        return ServiceSpec(
            service_id=token,
            package=token,
            modules=(module,),
            kind="cloud",
        )
    return None


def service_for_module(module: str, specs: tuple[ServiceSpec, ...]) -> ServiceSpec | None:
    """Longest module or prefix match. Family specs become one product."""
    target = (module or "").strip()
    if not target:
        return None
    best: ServiceSpec | None = None
    best_len = -1
    for spec in specs:
        for name in (*spec.modules, *spec.module_prefixes):
            if target == name or target.startswith(name + "."):
                if len(name) > best_len:
                    best = _concretize(spec, target) if spec.family else spec
                    best_len = len(name)
    return best


def _concretize(spec: ServiceSpec, target: str) -> ServiceSpec:
    parts = [part for part in target.split(".") if part]
    if spec.family == "google-cloud" and len(parts) >= 3 and parts[0] == "google" and parts[1] == "cloud":
        product = parts[2]
        package = "google-cloud-" + product.replace("_", "-")
        return replace(
            spec,
            service_id=package,
            package=package,
            modules=(f"google.cloud.{product}",),
            module_prefixes=(),
            family="",
        )
    if spec.family == "azure" and len(parts) >= 2 and parts[0] == "azure":
        rest = parts[1:3]
        package = "azure-" + "-".join(part.replace("_", "-") for part in rest)
        return replace(
            spec,
            service_id=package,
            package=package,
            modules=(".".join(parts[: 1 + len(rest)]),),
            module_prefixes=(),
            family="",
        )
    return spec


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
