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
