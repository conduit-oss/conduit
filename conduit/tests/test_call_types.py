"""Return types, attribute types, and the annotation cycle guard."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from conduit.impact.api import diff_wheels, load_api
from conduit.impact.model import ApiChange
from conduit.impact.intersect import intersect
from conduit.impact.report import format_impacts
from conduit.outside_in.graph import analyze_python
from conduit.outside_in.specs import ServiceSpec


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _ledger(root: Path) -> Path:
    lib = root / "lib"
    _write(lib / "ledger" / "__init__.py", "")
    _write(
        lib / "ledger" / "orm" / "__init__.py",
        """
        from .session import Session, sessionmaker
        from .query import Query
        """,
    )
    _write(
        lib / "ledger" / "orm" / "query.py",
        """
        def _generative(*assertions):
            def generate(fn, *args, **kw):
                self = args[0]._clone()
                fn(self, *args[1:], **kw)
                return self

            return generate

        class Query:
            def __init__(self, entities, session=None):
                self.session = session

            def select_entity_from(self, entity):
                return self

            def from_self(self, *cols):
                cloned = self.filter(*cols)
                return cloned

            @_generative
            def filter(self, *criterion):
                criterion = list(criterion)
        """,
    )
    _write(
        lib / "ledger" / "orm" / "session.py",
        """
        from .query import Query

        class Session:
            def __init__(self, query_cls=None):
                self._query_cls = query_cls if query_cls else Query

            def query(self, *entities, **kwargs):
                return self._query_cls(entities, self, **kwargs)

        class sessionmaker:
            def __init__(self, bind=None, class_=Session):
                self.class_ = type(class_.__name__, (class_,), {})

            def __call__(self, **local_kw):
                return self.class_(**local_kw)
        """,
    )
    return lib


def _spec() -> ServiceSpec:
    return ServiceSpec(
        service_id="ledger",
        package="ledger",
        modules=("ledger",),
        kind="database",
    )


def _methods(root: Path) -> dict[tuple[str, int], str]:
    lib = _ledger(root)
    sys.path.insert(0, str(lib))
    try:
        mapped = analyze_python(root / "app", (_spec(),))
    finally:
        sys.path.remove(str(lib))
    found: dict[tuple[str, int], str] = {}
    for chain in mapped.chains:
        if chain.via_wrapper:
            continue
        found.setdefault((chain.call_rel, chain.call_lineno), chain.sdk_method)
    return found


def test_chained_call_uses_the_library_return_class(tmp_path: Path):
    _write(
        tmp_path / "app" / "repo.py",
        """
        from ledger.orm import Session

        class Item:
            pass

        class Repo:
            def __init__(self, db: Session):
                self.db = db

            def listed(self):
                return self.db.query(Item).select_entity_from(Item)

            def annotated(self):
                self.store: Session
                return self.store.query(Item).from_self(Item)

            def narrowed(self):
                query = self.db.query(Item).from_self(Item)
                return query.select_entity_from(Item)
        """,
    )
    methods = list(_methods(tmp_path).values())
    assert sum(method.endswith("Query.select_entity_from") for method in methods) >= 2
    assert any(method.endswith("Query.from_self") for method in methods)


def test_star_import_from_a_nested_root_types_the_parameter(tmp_path: Path):
    _write(
        tmp_path / "app" / "service" / "methods" / "sessions.py",
        """
        from contextlib import contextmanager

        from ledger.orm import sessionmaker

        factory = sessionmaker()

        @contextmanager
        def scope():
            session = factory()
            yield session
        """,
    )
    _write(
        tmp_path / "app" / "service" / "methods" / "api.py",
        """
        from methods.sessions import scope
        """,
    )
    _write(
        tmp_path / "app" / "service" / "methods" / "work.py",
        """
        from methods.api import *

        class Item:
            pass

        def use(session):
            return session.query(Item).select_entity_from(Item)

        def caller():
            with scope() as session:
                return use(session)
        """,
    )
    methods = set(_methods(tmp_path).values())
    assert any(method.endswith("Query.select_entity_from") for method in methods)


def test_untyped_parameter_follows_a_context_manager(tmp_path: Path):
    _write(
        tmp_path / "app" / "scope.py",
        """
        from contextlib import contextmanager

        from ledger.orm import sessionmaker

        class Item:
            pass

        @contextmanager
        def scope():
            factory = sessionmaker()
            session = factory()
            yield session

        def use(session):
            return session.query(Item).select_entity_from(Item)

        def caller():
            with scope() as session:
                return use(session)
        """,
    )
    methods = set(_methods(tmp_path).values())
    assert any(method.endswith("Query.select_entity_from") for method in methods)


def test_multiline_method_is_reported_on_the_method_line(tmp_path: Path):
    source = """
        from ledger.orm import Session

        class Item:
            pass

        class Repo:
            def __init__(self, db: Session):
                self.db = db

            def listed(self):
                return (
                    self.db.query(Item)
                    .select_entity_from(Item)
                )
        """
    _write(tmp_path / "app" / "repo.py", source)
    methods = _methods(tmp_path)
    text = (tmp_path / "app" / "repo.py").read_text(encoding="utf-8")
    file_line = next(
        index for index, line in enumerate(text.splitlines(), start=1) if "select_entity_from" in line
    )
    hits = [line for (rel, line), method in methods.items() if method.endswith("Query.select_entity_from")]
    assert hits == [file_line]


def test_reassignment_keeps_the_value_that_reaches_the_call(tmp_path: Path):
    _write(
        tmp_path / "app" / "windows.py",
        """
        from ledger.orm import sessionmaker

        class Item:
            pass

        class Store:
            def __init__(self):
                factory = sessionmaker()
                self.session = factory()

            def _column_windows(self, session, column):
                return session.query(column).from_self(column)

            def _windowed_query(self, q, column):
                return self._column_windows(q.session, column)

            def run(self):
                documents = self.session.query(Item).filter(Item)
                documents = documents.filter(Item)
                return self._windowed_query(documents, Item)
        """,
    )
    methods = set(_methods(tmp_path).values())
    assert any(method.endswith("Query.from_self") for method in methods)


def test_attribute_on_a_returned_instance_types_the_next_parameter(tmp_path: Path):
    _write(
        tmp_path / "app" / "windows.py",
        """
        from ledger.orm import sessionmaker

        class Item:
            pass

        class Store:
            def __init__(self):
                factory = sessionmaker()
                self.session = factory()

            def _column_windows(self, session, column):
                return session.query(column).from_self(column)

            def _windowed_query(self, q, column):
                return self._column_windows(q.session, column)

            def run(self):
                documents = self.session.query(Item)
                return self._windowed_query(documents, Item)
        """,
    )
    methods = set(_methods(tmp_path).values())
    assert any(method.endswith("Query.from_self") for method in methods)


def test_parameter_annotation_matching_its_name_does_not_recurse(tmp_path: Path):
    _write(
        tmp_path / "row.py",
        """
        class Row:
            @classmethod
            def from_dict(cls, dict: dict):
                return cls(**dict)

            def nested(self, value: list):
                return self.from_dict(value)
        """,
    )
    analyze_python(tmp_path, ())


def test_deprecated_calls_are_a_separate_section(tmp_path: Path):
    _write(
        tmp_path / "app" / "repo.py",
        """
        from ledger.orm import Session

        class Item:
            pass

        def listed(db: Session):
            return db.query(Item).select_entity_from(Item)
        """,
    )
    lib = _ledger(tmp_path)
    sys.path.insert(0, str(lib))
    try:
        chains = [chain for chain in analyze_python(tmp_path / "app", (_spec(),)).chains if not chain.via_wrapper]
    finally:
        sys.path.remove(str(lib))
    change = ApiChange(
        kind="deprecated",
        symbol="ledger.orm.query.Query.select_entity_from",
        old="select_entity_from",
        new="select_entity_from",
        detail="Query.select_entity_from deprecated",
        confidence="low",
        source="wheel",
    )
    impacts = intersect(
        [change],
        chains,
        dependency="ledger",
        from_version="1",
        to_version="2",
    )
    assert impacts
    assert all(item.kind == "deprecated" for item in impacts)
    text = format_impacts(impacts)
    assert text.startswith("No call sites in this repo are hit by a breaking change.")
    assert "Deprecations" in text
    assert "Query.select_entity_from deprecated" in text


def test_lazy_getattr_keeps_a_moved_symbol(tmp_path: Path):
    old = tmp_path / "old" / "pkg"
    new = tmp_path / "new" / "pkg"
    _write(
        old / "__init__.py",
        """
        from pkg.error import Missing
        """,
    )
    _write(
        old / "error.py",
        """
        class Missing(Exception):
            pass
        """,
    )
    _write(
        new / "_error.py",
        """
        class Missing(Exception):
            pass
        """,
    )
    _write(
        new / "__init__.py",
        """
        _import_map = {"error": ("pkg._error", True), "Missing": ("pkg._error", False)}

        def __getattr__(name):
            target, is_submodule = _import_map[name]
            if is_submodule:
                return target
            return name
        """,
    )
    api = load_api(tmp_path / "new")
    assert "pkg.error.Missing" in api
    assert "pkg.Missing" in api
    removed = [item.symbol for item in diff_wheels(tmp_path / "old", tmp_path / "new") if item.kind == "removed"]
    assert "pkg.error.Missing" not in removed
    assert "pkg.Missing" not in removed


def test_inherited_reexported_method_is_not_removed(tmp_path: Path):
    """A method that moved onto a base, then re-exported, is still on the class.

    The class may be generic (``Base["T"]``), the base may live in another
    module, and the public module may pull the class in with ``import *``
    limited by ``__all__``. A class moved into a private module and imported
    again (the shape of an error type re-exported from the package) is the
    same re-export, not a removal.
    """
    _write(
        tmp_path / "old" / "pkg" / "cursor.py",
        """
        class Cursor:
            def skip(self, count):
                return self

            def limit(self, count):
                return self
        """,
    )
    _write(tmp_path / "old" / "pkg" / "__init__.py", "from pkg.cursor import Cursor\n")
    _write(
        tmp_path / "new" / "pkg" / "shared.py",
        """
        class _Base:
            def skip(self, count):
                return self

            def limit(self, count):
                return self
        """,
    )
    _write(
        tmp_path / "new" / "pkg" / "sync.py",
        """
        from pkg.shared import _Base

        class Cursor(_Base["Cursor"]):
            pass
        """,
    )
    _write(
        tmp_path / "new" / "pkg" / "cursor.py",
        """
        from pkg.sync import *

        __all__ = ["Cursor"]
        """,
    )
    _write(
        tmp_path / "new" / "pkg" / "_errors.py",
        """
        class Missing(Exception):
            pass
        """,
    )
    _write(
        tmp_path / "new" / "pkg" / "__init__.py",
        """
        from pkg.cursor import Cursor
        from pkg._errors import Missing

        __all__ = ["Cursor", "Missing"]
        """,
    )
    api = load_api(tmp_path / "new")
    assert "pkg.cursor.Cursor.skip" in api
    assert "pkg.cursor.Cursor.limit" in api
    assert "pkg.sync.Cursor.skip" in api
    assert "pkg.Missing" in api
    removed = [item.symbol for item in diff_wheels(tmp_path / "old", tmp_path / "new") if item.kind == "removed"]
    assert "pkg.cursor.Cursor.skip" not in removed
    assert "pkg.cursor.Cursor.limit" not in removed
    assert "pkg.Missing" not in removed


def test_subclass_field_read_through_super_is_the_base_value(tmp_path: Path):
    """``cls.field = super().field[key]`` is the subscript result, not the base class.

    The base stores a client subscript. A missing attribute on that type is
    another class, and that class name is also a constructor tail. The call on
    the subclass field is that class's method.
    """
    lib = tmp_path / "lib"
    _write(
        lib / "shelf" / "__init__.py",
        """
        from .client import Bin, Client, Database
        """,
    )
    _write(
        lib / "shelf" / "client.py",
        """
        class Bin:
            def insert(self, record, info=None):
                return record

            def update(self, key, record, info=None):
                return record

        class Database:
            def __getitem__(self, name):
                return Bin()

            def __getattr__(self, name):
                return Bin()

        class Client:
            def __getitem__(self, name):
                return Database()
        """,
    )
    _write(
        tmp_path / "app" / "tables.py",
        """
        from shelf.client import Client

        class Store:
            @classmethod
            def initialize(cls):
                cls._client = Client()
                cls._database = cls._client["main"]

        class Child(Store):
            @classmethod
            def initialize(cls):
                cls._database = super()._database["files"]

            @classmethod
            def write(cls, record, info):
                cls._database.insert(record, info)

            @classmethod
            def revise(cls, key, record, info):
                cls._database.update(key, record, info)
        """,
    )
    spec = ServiceSpec(
        service_id="shelf",
        package="shelf",
        modules=("shelf",),
        kind="database",
        constructor_tails=("Bin",),
    )
    sys.path.insert(0, str(lib))
    try:
        mapped = analyze_python(tmp_path / "app", (spec,))
    finally:
        sys.path.remove(str(lib))
    found = {
        (chain.call_rel, chain.call_lineno): (chain.sdk_method, chain.kind)
        for chain in mapped.chains
        if not chain.via_wrapper
    }
    text = (tmp_path / "app" / "tables.py").read_text(encoding="utf-8")
    insert_line = next(index for index, line in enumerate(text.splitlines(), start=1) if ".insert(" in line)
    update_line = next(index for index, line in enumerate(text.splitlines(), start=1) if ".update(" in line)
    assert found[("tables.py", insert_line)] == ("shelf.client.Bin.insert", "db_call")
    assert found[("tables.py", update_line)] == ("shelf.client.Bin.update", "db_call")


def test_same_file_line_and_change_is_emitted_once():
    from conduit.outside_in.model import Chain

    chain = Chain(
        service="shelf",
        package="shelf",
        call_rel="views.py",
        call_lineno=549,
        call_symbol="write",
        call_expr="Invoice.upcoming",
        hops=(),
        sdk_method="shelf.billing.Invoice.upcoming",
        kind="sdk_call",
        params=(),
        site_keywords=(),
        response_fields=(),
        confidence="high",
        via_wrapper=False,
    )
    removed = ApiChange(
        kind="removed",
        symbol="billing.Invoice.upcoming",
        old="upcoming",
        new="",
        detail="Invoice.upcoming removed",
        confidence="high",
        source="wheel",
    )
    alias = ApiChange(
        kind="removed",
        symbol="shelf.billing.Invoice.upcoming",
        old="upcoming",
        new="",
        detail="Invoice.upcoming removed",
        confidence="high",
        source="wheel",
    )
    renamed = ApiChange(
        kind="renamed",
        symbol="shelf.billing.Invoice.upcoming",
        old="upcoming",
        new="preview",
        detail="Invoice.upcoming renamed",
        confidence="high",
        source="wheel",
    )
    impacts = intersect(
        [removed, alias, renamed],
        [chain],
        dependency="shelf",
        from_version="1",
        to_version="2",
    )
    details = [item.change for item in impacts]
    assert details.count("Invoice.upcoming removed") == 1
    assert "Invoice.upcoming renamed" in details
    removed_impact = next(item for item in impacts if item.change == "Invoice.upcoming removed")
    assert len(removed_impact.sites) == 1
    assert removed_impact.sites[0].line == 549


def test_type_checker_class_replaces_the_receiver_when_it_is_concrete(tmp_path: Path, monkeypatch):
    _write(
        tmp_path / "app" / "tables.py",
        """
        from shelf.client import Client

        class Store:
            def __init__(self):
                self.db = Client()

            def write(self, record):
                self.db.insert(record)
        """,
    )

    class _Fake:
        def classes_at(self, path, line, col):
            return ("shelf.client.Bin",)

        def close(self):
            pass

    monkeypatch.setenv("CONDUIT_TYPES", "pyright")
    monkeypatch.setattr("conduit.outside_in.typecheck.open_session", lambda root: _Fake())
    spec = ServiceSpec(
        service_id="shelf",
        package="shelf",
        modules=("shelf",),
        kind="database",
        constructor_tails=("Bin",),
    )
    mapped = analyze_python(tmp_path / "app", (spec,))
    methods = [chain.sdk_method for chain in mapped.chains if not chain.via_wrapper]
    assert "shelf.client.Bin.insert" in methods


def test_type_checker_unknown_keeps_the_hand_written_resolver(tmp_path: Path, monkeypatch):
    _write(
        tmp_path / "app" / "repo.py",
        """
        from ledger.orm import Session

        class Item:
            pass

        def listed(db: Session):
            return db.query(Item).select_entity_from(Item)
        """,
    )

    class _Fake:
        def classes_at(self, path, line, col):
            return ()

        def close(self):
            pass

    monkeypatch.setenv("CONDUIT_TYPES", "pyright")
    monkeypatch.setattr("conduit.outside_in.typecheck.open_session", lambda root: _Fake())
    methods = set(_methods(tmp_path).values())
    assert any(method.endswith("Query.select_entity_from") for method in methods)


def test_qualname_comes_from_an_installed_class_file():
    from conduit.outside_in.typecheck import hover_is_only_unknown, qualname_from_uri

    uri = "file:///tmp/venvs/spike/lib/python3.12/site-packages/shelf/client.py"
    assert qualname_from_uri(uri, "Bin") == "shelf.client.Bin"
    assert qualname_from_uri("file:///tmp/app/tables.py", "Bin") is None
    assert hover_is_only_unknown("(variable) field: Unknown")
    assert hover_is_only_unknown("(variable) field: Any | None")
    assert not hover_is_only_unknown("(variable) field: Bin | Unknown")
    assert not hover_is_only_unknown("")


def test_dynamically_added_attribute_is_not_removed(tmp_path: Path):
    _write(
        tmp_path / "old" / "pkg" / "__init__.py",
        """
        class Cursor:
            def limit(self, count):
                return self
        """,
    )
    _write(
        tmp_path / "new" / "pkg" / "__init__.py",
        """
        class Cursor:
            pass

        def limit(self, count):
            return self

        setattr(Cursor, "limit", limit)
        Cursor.skip = limit
        """,
    )
    api = load_api(tmp_path / "new")
    assert "pkg.Cursor.limit" in api
    assert "pkg.Cursor.skip" in api
    removed = [item.symbol for item in diff_wheels(tmp_path / "old", tmp_path / "new") if item.kind == "removed"]
    assert "pkg.Cursor.limit" not in removed
