"""ImportLedger: sole owner of Import / ImportFrom surgery for one module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import libcst as cst

from conduit.packet.declaration_rules import Symbol

BindingKind = Literal["from_import", "module_import", "star_import"]
RefusalKind = Literal[
    "star_import", "relative_move", "guarded_import", "ambiguous_binding", "unparseable"
]


@dataclass(frozen=True)
class Binding:
    local: str
    symbol: Symbol
    kind: BindingKind
    aliased: bool
    lineno: int


@dataclass(frozen=True)
class Refusal:
    kind: RefusalKind
    lineno: int
    detail: str


@dataclass(frozen=True)
class Rebind:
    bindings_changed: int
    renamed_locals: tuple[tuple[str, str], ...]
    refusals: tuple[Refusal, ...]


def dotted_name(node: cst.BaseExpression | cst.Name | None) -> str:
    if node is None:
        return ""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        parts: list[str] = []
        cur: cst.BaseExpression | None = node
        while isinstance(cur, cst.Attribute):
            parts.append(cur.attr.value)
            cur = cur.value
        if isinstance(cur, cst.Name):
            parts.append(cur.value)
            return ".".join(reversed(parts))
    return ""


def make_module(dotted: str) -> cst.BaseExpression:
    parts = dotted.split(".")
    node: cst.BaseExpression = cst.Name(parts[0])
    for part in parts[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(part))
    return node


@dataclass
class _PendingSplit:
    to_module: str
    name: str
    asname: str | None


@dataclass
class ImportLedger:
    module: cst.Module
    _refusals: list[Refusal] = field(default_factory=list)
    _pending_splits: list[_PendingSplit] = field(default_factory=list)
    _ensure: list[Symbol] = field(default_factory=list)
    changes: int = 0
    _renamed_locals: list[tuple[str, str]] = field(default_factory=list)

    def bindings_of(self, symbol: Symbol) -> tuple[Binding, ...]:
        found: list[Binding] = []
        for stmt in self.module.body:
            if not isinstance(stmt, cst.SimpleStatementLine):
                continue
            for small in stmt.body:
                if not isinstance(small, cst.ImportFrom):
                    continue
                if small.relative or small.module is None:
                    continue
                if dotted_name(small.module) != symbol.module:
                    continue
                if isinstance(small.names, cst.ImportStar):
                    found.append(
                        Binding(
                            local="*",
                            symbol=symbol,
                            kind="star_import",
                            aliased=False,
                            lineno=0,
                        )
                    )
                    continue
                if not isinstance(small.names, (list, tuple)):
                    continue
                for alias in small.names:
                    if not isinstance(alias, cst.ImportAlias):
                        continue
                    if not isinstance(alias.name, cst.Name):
                        continue
                    if alias.name.value != symbol.name:
                        continue
                    local = (
                        alias.asname.name.value
                        if alias.asname and isinstance(alias.asname.name, cst.Name)
                        else alias.name.value
                    )
                    found.append(
                        Binding(
                            local=local,
                            symbol=symbol,
                            kind="from_import",
                            aliased=alias.asname is not None,
                            lineno=0,
                        )
                    )
        return tuple(found)

    def rebind(self, old: Symbol, new: Symbol) -> Rebind:
        refusals: list[Refusal] = []
        renamed: list[tuple[str, str]] = []
        changed = 0
        pending = self._pending_splits

        class _Rewrite(cst.CSTTransformer):
            def leave_ImportFrom(
                self, original: cst.ImportFrom, updated: cst.ImportFrom
            ) -> cst.BaseSmallStatement | cst.RemovalSentinel:
                nonlocal changed
                if updated.relative:
                    if isinstance(updated.names, (list, tuple)):
                        for alias in updated.names:
                            if (
                                isinstance(alias, cst.ImportAlias)
                                and isinstance(alias.name, cst.Name)
                                and alias.name.value == old.name
                            ):
                                refusals.append(
                                    Refusal(
                                        kind="relative_move",
                                        lineno=0,
                                        detail="relative import move refused",
                                    )
                                )
                    return updated
                if updated.module is None:
                    return updated
                if dotted_name(updated.module) != old.module:
                    return updated
                if isinstance(updated.names, cst.ImportStar):
                    refusals.append(
                        Refusal(
                            kind="star_import",
                            lineno=0,
                            detail=f"star import of {old.module}",
                        )
                    )
                    return updated
                if not isinstance(updated.names, (list, tuple)):
                    return updated

                names = list(updated.names)
                dest_present = any(
                    isinstance(a, cst.ImportAlias)
                    and isinstance(a.name, cst.Name)
                    and a.name.value == new.name
                    for a in names
                )
                kept: list[cst.ImportAlias] = []
                touched = False
                for alias in names:
                    if not isinstance(alias, cst.ImportAlias) or not isinstance(
                        alias.name, cst.Name
                    ):
                        kept.append(alias)
                        continue
                    if alias.name.value != old.name:
                        kept.append(alias)
                        continue
                    touched = True
                    asname = alias.asname
                    if old.module == new.module:
                        if dest_present:
                            changed += 1
                            continue
                        kept.append(alias.with_changes(name=cst.Name(new.name)))
                        changed += 1
                        if asname is None:
                            renamed.append((old.name, new.name))
                    else:
                        pending.append(
                            _PendingSplit(
                                to_module=new.module,
                                name=new.name,
                                asname=(
                                    asname.name.value
                                    if asname and isinstance(asname.name, cst.Name)
                                    else None
                                ),
                            )
                        )
                        changed += 1
                if not touched:
                    return updated
                if not kept:
                    return cst.RemovalSentinel.REMOVE
                return updated.with_changes(names=kept)

        self.module = self.module.visit(_Rewrite())
        self.changes += changed
        self._renamed_locals.extend(renamed)
        self._refusals.extend(refusals)
        return Rebind(
            bindings_changed=changed,
            renamed_locals=tuple(renamed),
            refusals=tuple(refusals),
        )

    def ensure(self, symbol: Symbol) -> str:
        for b in self.bindings_of(symbol):
            if b.kind == "from_import":
                return b.local
        self._ensure.append(symbol)
        return symbol.name

    def commit(self) -> cst.Module:
        if not self._pending_splits and not self._ensure:
            return self.module

        body = list(self.module.body)

        def _merge_or_insert(module_name: str, name: str, asname: str | None) -> None:
            nonlocal body
            for i, stmt in enumerate(body):
                if not isinstance(stmt, cst.SimpleStatementLine):
                    continue
                for j, small in enumerate(stmt.body):
                    if not isinstance(small, cst.ImportFrom):
                        continue
                    if small.relative or small.module is None:
                        continue
                    if dotted_name(small.module) != module_name:
                        continue
                    if isinstance(small.names, cst.ImportStar):
                        continue
                    names = (
                        list(small.names)
                        if isinstance(small.names, (list, tuple))
                        else []
                    )
                    if any(
                        isinstance(a, cst.ImportAlias)
                        and isinstance(a.name, cst.Name)
                        and a.name.value == name
                        for a in names
                    ):
                        return
                    names.append(
                        cst.ImportAlias(
                            name=cst.Name(name),
                            asname=(
                                cst.AsName(name=cst.Name(asname)) if asname else None
                            ),
                        )
                    )
                    new_body = list(stmt.body)
                    new_body[j] = small.with_changes(names=names)
                    body[i] = stmt.with_changes(body=new_body)
                    self.changes += 1
                    return
            imp = cst.ImportFrom(
                module=make_module(module_name),
                names=[
                    cst.ImportAlias(
                        name=cst.Name(name),
                        asname=cst.AsName(name=cst.Name(asname)) if asname else None,
                    )
                ],
            )
            body.insert(0, cst.SimpleStatementLine(body=[imp]))
            self.changes += 1

        for pending in self._pending_splits:
            _merge_or_insert(pending.to_module, pending.name, pending.asname)
        for sym in self._ensure:
            _merge_or_insert(sym.module, sym.name, None)

        self._pending_splits.clear()
        self._ensure.clear()
        self.module = self.module.with_changes(body=body)
        return self.module


def rewrite_import_member(content: str, old: Symbol, new: Symbol) -> tuple[str, int]:
    """AST-only import-member rewrite. No string fallback."""
    if old.name not in content and old.module not in content:
        return content, 0
    try:
        module = cst.parse_module(content)
    except Exception:
        return content, 0
    ledger = ImportLedger(module=module)
    rebind = ledger.rebind(old, new)
    if rebind.bindings_changed == 0 and not ledger._pending_splits:
        return content, 0
    committed = ledger.commit()
    code = committed.code
    if code == content:
        return content, 0
    return code, max(rebind.bindings_changed, 1)
