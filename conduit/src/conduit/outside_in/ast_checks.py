"""Single-pass AST check plug-ins for distribution evidence scanning.

Each check declares the node types it cares about, receives those nodes during
one ``ast.walk``, and exposes ``finish()`` for its result. Adding a check is
registering one class — the walker does not grow another full walk.
"""

from __future__ import annotations

import ast
import re
from typing import Callable, ClassVar, Iterable


class AstCheck:
    """One extraction over a module AST. Subclass and set ``wants``."""

    wants: ClassVar[tuple[type[ast.AST], ...]] = ()

    def visit(self, node: ast.AST) -> None:
        return None

    def finish(self):
        raise NotImplementedError


def run_checks(tree: ast.AST, checks: Iterable[AstCheck]) -> None:
    """Walk ``tree`` once, dispatching each node to interested checks."""
    index: dict[type[ast.AST], list[AstCheck]] = {}
    ordered = list(checks)
    for check in ordered:
        for typ in check.wants:
            index.setdefault(typ, []).append(check)
    for node in ast.walk(tree):
        for check in index.get(type(node), ()):
            check.visit(node)


class AliasesCheck(AstCheck):
    """Import aliases and names bound to this distribution's own modules."""

    wants = (ast.Import, ast.ImportFrom)

    def __init__(
        self,
        own: set[str],
        *,
        stdlib_io: dict[str, str],
        is_our_module: Callable[[str, set[str]], bool],
    ) -> None:
        self._own = own
        self._stdlib_io = stdlib_io
        self._is_our_module = is_our_module
        self.aliases: dict[str, str] = {}
        self.internal: set[str] = set()

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                self.aliases[local] = self._stdlib_io.get(alias.name, alias.name)
                if self._is_our_module(alias.name, self._own):
                    self.internal.add(local)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                for alias in node.names:
                    self.internal.add(alias.asname or alias.name)
                return
            if not node.module:
                return
            base = self._stdlib_io.get(node.module, node.module)
            inside = self._is_our_module(node.module, self._own)
            for alias in node.names:
                local = alias.asname or alias.name
                self.aliases[local] = f"{base}.{alias.name}"
                if inside:
                    self.internal.add(local)

    def finish(self) -> tuple[dict[str, str], set[str]]:
        return self.aliases, self.internal


class PathTemplatesCheck(AstCheck):
    """Fixed API path suffixes after a templated host."""

    wants = (ast.JoinedStr, ast.Call, ast.BinOp)

    def __init__(
        self,
        *,
        paths_from_joined: Callable[[list[ast.AST]], set[str]],
        paths_from_format_string: Callable[[str], set[str]],
        const_str: Callable[[ast.AST | None], str],
    ) -> None:
        self._paths_from_joined = paths_from_joined
        self._paths_from_format_string = paths_from_format_string
        self._const_str = const_str
        self.found: set[str] = set()

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.JoinedStr):
            self.found.update(self._paths_from_joined(node.values))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            template = self._const_str(node.func.value)
            if template:
                self.found.update(self._paths_from_format_string(template))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            template = self._const_str(node.left)
            if template:
                self.found.update(self._paths_from_format_string(template))

    def finish(self) -> set[str]:
        return self.found


class ClientSurfaceCheck(AstCheck):
    """Product Client/Session or request-URL construction in this module.

    Path templates from ``PathTemplatesCheck`` are reused in ``finish`` — this
    check does not walk the tree again for f-string / format URL shapes.
    """

    wants = (ast.ClassDef,)

    def __init__(
        self,
        path_templates: PathTemplatesCheck,
        *,
        client_type: Callable[[str], bool],
        method_names: Callable[[ast.ClassDef], set[str]],
        http_surface: frozenset[str],
    ) -> None:
        self._path_templates = path_templates
        self._client_type = client_type
        self._method_names = method_names
        self._http_surface = http_surface
        self._product_client = False

    def visit(self, node: ast.AST) -> None:
        if not isinstance(node, ast.ClassDef) or not self._client_type(node.name):
            return
        methods = self._method_names(node)
        public = {
            method
            for method in methods
            if not method.startswith("_") and method not in {"close", "aclose"}
        }
        if public and not (public <= (self._http_surface | {"send_request", "open"})):
            self._product_client = True

    def finish(self) -> bool:
        if self._product_client:
            return True
        # Reuse templates already collected in the same pass.
        return bool(self._path_templates.found)


class AssignedHostsCheck(AstCheck):
    """Hosts that feed this module's runtime client surface."""

    wants = (ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef)

    def __init__(
        self,
        surface: ClientSurfaceCheck,
        *,
        urlish_identifier: Callable[[str], bool],
        host_of: Callable[[str], str],
        url_literal_host: Callable[[str], str],
    ) -> None:
        self._surface = surface
        self._urlish_identifier = urlish_identifier
        self._host_of = host_of
        self._url_literal_host = url_literal_host
        self._pending_named: list[ast.AST] = []
        self._pending_plain: list[ast.AST] = []
        self.hosts: set[str] = set()

    def _named(self, target: ast.AST) -> bool:
        if isinstance(target, ast.Name):
            return self._urlish_identifier(target.id)
        if isinstance(target, ast.Attribute):
            return self._urlish_identifier(target.attr)
        return False

    def _plain_url_constant(self, node: ast.AST | None) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return bool(self._url_literal_host(node.value))
        if isinstance(node, ast.IfExp):
            return self._plain_url_constant(node.body) or self._plain_url_constant(node.orelse)
        return False

    def _take(self, node: ast.AST | None) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            host = self._host_of(node.value)
            if host:
                self.hosts.add(host)
        elif isinstance(node, ast.IfExp):
            self._take(node.body)
            self._take(node.orelse)
        elif isinstance(node, ast.Call):
            for arg in node.args:
                self._take(arg)
            for keyword in node.keywords:
                if keyword.value is not None:
                    self._take(keyword.value)

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.Assign):
            if any(self._named(target) for target in node.targets):
                self._pending_named.append(node.value)
            elif self._plain_url_constant(node.value):
                self._pending_plain.append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.target is not None and node.value is not None:
            if self._named(node.target):
                self._pending_named.append(node.value)
            elif self._plain_url_constant(node.value):
                self._pending_plain.append(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            defaults = [*args.defaults]
            positional = [arg.arg for arg in (*args.posonlyargs, *args.args)]
            for arg_name, default in zip(positional[-len(defaults) :], defaults):
                if self._named(ast.Name(id=arg_name)):
                    self._pending_named.append(default)
                elif self._plain_url_constant(default):
                    self._pending_plain.append(default)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if default is None:
                    continue
                if self._named(ast.Name(id=arg.arg)):
                    self._pending_named.append(default)
                elif self._plain_url_constant(default):
                    self._pending_plain.append(default)

    def finish(self) -> set[str]:
        for value in self._pending_named:
            self._take(value)
        if self._surface.finish():
            for value in self._pending_plain:
                self._take(value)
        return self.hosts


class UsesStructCheck(AstCheck):
    """Binary framing via struct.pack / unpack."""

    wants = (ast.Call,)

    def __init__(
        self,
        aliases: AliasesCheck,
        *,
        call_name: Callable[[ast.AST], str],
        expand: Callable[[str, dict[str, str]], str],
    ) -> None:
        self._aliases = aliases
        self._call_name = call_name
        self._expand = expand
        self.found = False

    def visit(self, node: ast.AST) -> None:
        if self.found or not isinstance(node, ast.Call):
            return
        expanded = self._expand(self._call_name(node.func), self._aliases.aliases)
        if expanded in {"struct.pack", "struct.unpack", "struct.pack_into", "struct.unpack_from"}:
            self.found = True

    def finish(self) -> bool:
        return self.found
