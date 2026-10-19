"""One-line-per-cell coverage grid table.

Columns:
  id, binding (A), reach (B), change (C), expect_flag (bool), xfail_reason (or "")
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cell:
    id: str
    binding: str
    reach: str
    change: str
    expect_flag: bool
    xfail_reason: str = ""


# Binding snippets: produce a Client (or call site) named ``c`` / call legacy_call.
# ``CALL`` is replaced with the breaking call expression for the change under test.
_BINDINGS: dict[str, str] = {
    "direct": "c = Client()\n{CALL}\n",
    "import_alias": "import zzgrid as z\nc = z.Client()\n{CALL}\n",
    "from_import": "from zzgrid import Client\nc = Client()\n{CALL}\n",
    "return_value": (
        "def make():\n"
        "    return Client()\n"
        "c = make()\n"
        "{CALL}\n"
    ),
    "param_typed": (
        "def use(c: Client):\n"
        "    {CALL}\n"
        "\n"
        "use(Client())\n"
    ),
    "param_untyped": (
        "def use(c):\n"
        "    {CALL}\n"
        "\n"
        "use(Client())\n"
    ),
    "self_init": (
        "class Holder:\n"
        "    def __init__(self):\n"
        "        self.c = Client()\n"
        "\n"
        "    def go(self):\n"
        "        c = self.c\n"
        "        {CALL}\n"
        "\n"
        "Holder().go()\n"
    ),
    "self_elsewhere": (
        "class Holder:\n"
        "    def setup(self):\n"
        "        self.c = Client()\n"
        "\n"
        "    def go(self):\n"
        "        c = self.c\n"
        "        {CALL}\n"
        "\n"
        "h = Holder()\n"
        "h.setup()\n"
        "h.go()\n"
    ),
    "class_attr": (
        "class Holder:\n"
        "    c = Client()\n"
        "\n"
        "    def go(self):\n"
        "        c = self.c\n"
        "        {CALL}\n"
        "\n"
        "Holder().go()\n"
    ),
    "module_global": None,  # special: two files
    "config_attr": (
        "class Settings:\n"
        "    client = Client()\n"
        "c = Settings.client\n"
        "{CALL}\n"
    ),
    "dict_container": (
        "bag = {\"c\": Client()}\n"
        "c = bag[\"c\"]\n"
        "{CALL}\n"
    ),
    "list_container": (
        "bag = [Client()]\n"
        "c = bag[0]\n"
        "{CALL}\n"
    ),
    "decorator": (
        "def wrap(fn):\n"
        "    def inner():\n"
        "        c = Client()\n"
        "        return fn(c)\n"
        "    return inner\n"
        "\n"
        "@wrap\n"
        "def go(c):\n"
        "    {CALL}\n"
        "\n"
        "go()\n"
    ),
    "subclass_super": (
        "class Sub(Base):\n"
        "    def run(self):\n"
        "        c = Client()\n"
        "        {CALL}\n"
        "        return super().run()\n"
        "\n"
        "Sub().run()\n"
    ),
    "factory": (
        "c = factory()\n"
        "{CALL}\n"
    ),
    "registry": (
        "c = REGISTRY[\"default\"]()\n"
        "{CALL}\n"
    ),
    "getattr_const": (
        "c = getattr(__import__(\"zzgrid\"), \"Client\")()\n"
        "{CALL}\n"
    ),
    "context_manager": (
        "from contextlib import contextmanager\n"
        "\n"
        "@contextmanager\n"
        "def open_client():\n"
        "    c = Client()\n"
        "    yield c\n"
        "\n"
        "with open_client() as c:\n"
        "    {CALL}\n"
    ),
    "async_await": (
        "import asyncio\n"
        "\n"
        "async def go():\n"
        "    c = Client()\n"
        "    {CALL}\n"
        "\n"
        "asyncio.run(go())\n"
    ),
}

_CHANGES: dict[str, tuple[str, str]] = {
    # change_id -> (call_expr_using_c, expect_flag for direct/same_function)
    "method_removed": ("c.legacy_call(\"/\")", "flag"),
    "renamed": ("c.rename_me()", "flag"),
    "required_param_added": ("c.needs_new_arg()", "flag"),
    "param_removed_kw": ("c.drop_kw(token=\"y\")", "flag"),
    "return_dict_to_object": ("c.dict_shape()[\"items\"]", "flag"),
    "kind_function_to_class": ("c.become_class()", "flag"),
    "attr_removed_on_return": ("c.attr_holder().gone", "flag"),
    "default_changed": ("c.soft_default()", "noflag"),  # deprecation only
}


def _call_line(change: str) -> str:
    expr, _ = _CHANGES[change]
    return expr


def user_sources(cell: Cell) -> dict[str, str]:
    """Return relative path -> source for the synthetic consumer."""
    expr = _call_line(cell.change)
    header = (
        "from zzgrid import Client, Base, factory, REGISTRY, connect\n"
        if cell.binding not in {"import_alias", "from_import", "getattr_const"}
        else (
            ""
            if cell.binding == "import_alias"
            else "from zzgrid import Client\n"
            if cell.binding == "from_import"
            else ""
        )
    )
    if cell.binding == "module_global":
        return {
            "libhelpers.py": (
                "from zzgrid import Client\n"
                "SHARED = Client()\n"
            ),
            "app.py": (
                "from libhelpers import SHARED\n"
                "c = SHARED\n"
                f"{expr}\n"
            ),
        }
    if cell.reach == "across_files":
        # binding in helpers, call in app
        bind = _BINDINGS[cell.binding]
        if bind is None:
            return user_sources(Cell(cell.id, "direct", "across_files", cell.change, cell.expect_flag))
        # put construction in helpers, call in app via function return
        return {
            "helpers.py": (
                "from zzgrid import Client, Base, factory, REGISTRY, connect\n"
                "def get_client():\n"
                "    return Client()\n"
            ),
            "app.py": (
                "from helpers import get_client\n"
                "c = get_client()\n"
                f"{expr}\n"
            ),
        }
    if cell.reach == "wrapper":
        return {
            "app.py": header
            + (
                "def wrapper(c):\n"
                f"    {expr}\n"
                "wrapper(Client())\n"
            )
        }
    if cell.reach == "method_chain":
        return {
            "app.py": header
            + (
                "c = Client()\n"
                f"c.ping()\n"
                f"{expr}\n"
            )
        }
    if cell.reach == "reassignment":
        return {
            "app.py": header
            + (
                "c = Client()\n"
                "d = c\n"
                "c = d\n"
                f"{expr}\n"
            )
        }
    if cell.reach == "closure":
        return {
            "app.py": header
            + (
                "def outer():\n"
                "    c = Client()\n"
                "    def inner():\n"
                f"        {expr}\n"
                "    return inner\n"
                "outer()()\n"
            )
        }
    if cell.reach == "lambda":
        return {
            "app.py": header
            + (
                "c = Client()\n"
                f"(lambda c: {expr})(c)\n"
            )
        }
    # same_function
    bind = _BINDINGS[cell.binding]
    if bind is None:
        return user_sources(Cell(cell.id, "direct", cell.reach, cell.change, cell.expect_flag))
    body = bind.replace("{CALL}", expr)
    # indent calls inside def bodies that already indent
    return {"app.py": header + body}


def expected_site(cell: Cell, sources: dict[str, str]) -> tuple[str, int] | None:
    """Exact file:line of the breaking call, or None when not flagged."""
    if not cell.expect_flag:
        return None
    needle = _call_line(cell.change).split("(")[0].split(".")[-1]
    # prefer the line containing the call expression fragment
    frag = _call_line(cell.change)
    for rel, text in sources.items():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if frag in line or (needle in line and "def " not in line):
                return rel, lineno
    # fallback: last non-empty line of app.py
    text = sources.get("app.py") or next(iter(sources.values()))
    rel = "app.py" if "app.py" in sources else next(iter(sources))
    lines = [i for i, line in enumerate(text.splitlines(), 1) if line.strip()]
    return rel, lines[-1]


def build_cells() -> list[Cell]:
    cells: list[Cell] = []

    # Every A × method_removed × {same_function, across_files}
    for binding in _BINDINGS:
        for reach in ("same_function", "across_files"):
            # Known hard cases marked xfail when Conduit historically misses them.
            xfail = ""
            expect = True
            if binding in {"dict_container", "list_container"}:
                xfail = "container values are not tracked to call sites"
            elif binding == "getattr_const":
                xfail = "dynamic getattr client construction is unresolved"
            elif binding == "async_await":
                xfail = "async client flows are not fully modeled"
            elif binding == "decorator" and reach == "same_function":
                xfail = "decorator-injected parameters often stay untyped"
            elif binding == "class_attr" and reach == "same_function":
                xfail = "class-body Client() assignment not followed on instance"
            elif binding == "config_attr" and reach == "same_function":
                xfail = "settings/config class attributes are not tracked"
            elif binding == "registry" and reach == "same_function":
                xfail = "registry dict lookup of a class is unresolved"
            cells.append(
                Cell(
                    id=f"A:{binding}|B:{reach}|C:method_removed",
                    binding=binding,
                    reach=reach,
                    change="method_removed",
                    expect_flag=expect,
                    xfail_reason=xfail,
                )
            )

    # Every C via direct + same_function
    for change, (_expr, flag) in _CHANGES.items():
        xfail = ""
        if change in {"return_dict_to_object", "kind_function_to_class", "attr_removed_on_return"}:
            xfail = "return-shape / kind changes need typed response tracking"
        elif change == "required_param_added":
            xfail = "missing required-arg calls are not yet flagged as breaks"
        cells.append(
            Cell(
                id=f"A:direct|B:same_function|C:{change}",
                binding="direct",
                reach="same_function",
                change=change,
                expect_flag=(flag == "flag"),
                xfail_reason=xfail,
            )
        )

    # Every B via direct + method_removed
    for reach in (
        "same_function",
        "wrapper",
        "method_chain",
        "reassignment",
        "across_files",
        "closure",
        "lambda",
    ):
        xfail = ""
        if reach in {"closure", "lambda"}:
            xfail = "closure/lambda captured clients are unresolved"
        cells.append(
            Cell(
                id=f"A:direct|B:{reach}|C:method_removed",
                binding="direct",
                reach=reach,
                change="method_removed",
                expect_flag=True,
                xfail_reason=xfail,
            )
        )

    # Deduplicate by id (A×removed×same_function overlaps B×direct×removed)
    uniq: dict[str, Cell] = {}
    for cell in cells:
        uniq[cell.id] = cell
    return list(uniq.values())
