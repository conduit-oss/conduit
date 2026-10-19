from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, UnsupportedCompilationError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import instance_state

from app.api.admin_entities_helpers import (
    FAMILY_RELATIONSHIP_TYPES,
    ORGANIZATION_RELATIONSHIP_TYPES,
    replace_contact_tags,
    replace_family_tags,
    replace_organization_tags,
    replace_service_instance_tags,
    require_assignable_tag,
    request_id,
    parse_contact_type_filter,
    parse_relationship_type,
)
from app.db.base import Base
from app.db.models import RelationshipType, ServiceInstanceTag
from app.db.models.contact import Contact
from app.db.models.enums import (
    ContactSource,
    ContactType,
    MailchimpSyncStatus,
    OrganizationType,
)
from app.db.models.family import Family
from app.db.models.organization import Organization
from app.db.models.tag import ContactTag, FamilyTag, OrganizationTag, Tag
from app.exceptions import ValidationError


def test_request_id_reads_api_gateway_request_id() -> None:
    event: dict[str, Any] = {
        "requestContext": {"requestId": "  req-abc  "},
    }
    assert request_id(event) == "req-abc"


def test_request_id_empty_when_missing() -> None:
    assert request_id({}) == ""


def test_parse_relationship_type_defaults_to_prospect() -> None:
    assert parse_relationship_type(None, field="x") == RelationshipType.PROSPECT
    assert parse_relationship_type("", field="x") == RelationshipType.PROSPECT


def test_parse_relationship_type_accepts_vendor() -> None:
    assert (
        parse_relationship_type("vendor", field="relationship_type")
        == RelationshipType.VENDOR
    )


def test_parse_relationship_type_accepts_client() -> None:
    assert (
        parse_relationship_type("client", field="relationship_type")
        == RelationshipType.CLIENT
    )


def test_parse_relationship_type_family_allowed_subset() -> None:
    assert (
        parse_relationship_type(
            "client",
            field="relationship_type",
            allowed=FAMILY_RELATIONSHIP_TYPES,
        )
        == RelationshipType.CLIENT
    )
    with pytest.raises(ValidationError, match="relationship_type"):
        parse_relationship_type(
            "past_client",
            field="relationship_type",
            allowed=FAMILY_RELATIONSHIP_TYPES,
        )


def test_parse_relationship_type_organization_excludes_past_client() -> None:
    assert (
        parse_relationship_type(
            "partner",
            field="relationship_type",
            allowed=ORGANIZATION_RELATIONSHIP_TYPES,
        )
        == RelationshipType.PARTNER
    )
    with pytest.raises(ValidationError, match="relationship_type"):
        parse_relationship_type(
            "past_client",
            field="relationship_type",
            allowed=ORGANIZATION_RELATIONSHIP_TYPES,
        )


def test_parse_contact_type_filter_empty_means_no_filter() -> None:
    assert parse_contact_type_filter(None) is None
    assert parse_contact_type_filter("") is None
    assert parse_contact_type_filter("  ") is None


def test_parse_contact_type_filter_accepts_known_values() -> None:
    assert parse_contact_type_filter("parent") == ContactType.PARENT
    assert parse_contact_type_filter("CHILD") == ContactType.CHILD


def test_parse_contact_type_filter_rejects_unknown() -> None:
    with pytest.raises(ValidationError, match="contact_type"):
        parse_contact_type_filter("not_a_type")


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_replace_service_instance_tags_dedupes_preserving_order() -> None:
    instance_id = uuid4()
    t1, t2 = uuid4(), uuid4()
    session = MagicMock()
    added: list[ServiceInstanceTag] = []

    def fake_get(_model: type, pk: object) -> object | None:
        if pk == t1:
            return SimpleNamespace(id=t1, archived_at=None)
        if pk == t2:
            return SimpleNamespace(id=t2, archived_at=None)
        return None

    session.get.side_effect = fake_get
    session.add.side_effect = lambda row: added.append(row)

    replace_service_instance_tags(
        session,
        instance_id=instance_id,
        tag_ids=[t2, t1, t2],
    )

    assert len(added) == 2
    assert isinstance(added[0], ServiceInstanceTag)
    assert added[0].service_instance_id == instance_id
    assert added[0].tag_id == t2
    assert added[1].tag_id == t1
    session.execute.assert_called_once()
    session.flush.assert_called_once()


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_replace_service_instance_tags_unknown_id_sets_field() -> None:
    instance_id = uuid4()
    known = uuid4()
    missing = uuid4()
    session = MagicMock()

    def fake_get(_model: type, pk: object) -> object | None:
        if pk == known:
            return SimpleNamespace(id=known, archived_at=None)
        return None

    session.get.side_effect = fake_get

    with pytest.raises(ValidationError, match="tag_id not found") as exc:
        replace_service_instance_tags(
            session,
            instance_id=instance_id,
            tag_ids=[known, missing],
        )
    assert exc.value.field == "tag_ids"


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_replace_service_instance_tags_rejects_archived_tag() -> None:
    instance_id = uuid4()
    active_id = uuid4()
    archived_id = uuid4()
    session = MagicMock()

    def fake_get(_model: type, pk: object) -> object | None:
        if pk == active_id:
            return SimpleNamespace(id=active_id, archived_at=None)
        if pk == archived_id:
            return SimpleNamespace(id=archived_id, archived_at="2024-01-01")
        return None

    session.get.side_effect = fake_get

    with pytest.raises(ValidationError, match="tag is archived") as exc:
        replace_service_instance_tags(
            session,
            instance_id=instance_id,
            tag_ids=[active_id, archived_id],
        )
    assert exc.value.field == "tag_ids"


def _push_sqlite_jsonb_compiler() -> Callable[[], None]:
    """Install a sqlite JSONB compiler and return a function that removes it.

    ``@compiles`` replaces ``JSONB._compiler_dispatch``. The previous callable
    is put back so a later test does not keep the sqlite rule, and so repeated
    sessions do not wrap the dispatcher in itself.
    """
    previous_dispatch = JSONB.__dict__.get("_compiler_dispatch")
    had_dispatcher = "_compiler_dispatcher" in JSONB.__dict__

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_for_sqlite(element: JSONB, compiler: Any, **kwargs: Any) -> str:
        return "JSON"

    def _restore() -> None:
        if previous_dispatch is not None:
            JSONB._compiler_dispatch = previous_dispatch
        elif "_compiler_dispatch" in JSONB.__dict__:
            delattr(JSONB, "_compiler_dispatch")
        if not had_dispatcher and "_compiler_dispatcher" in JSONB.__dict__:
            delattr(JSONB, "_compiler_dispatcher")

    return _restore


@contextmanager
def _tag_session() -> Iterator[Session]:
    """SQLite session for tag-link ORM behavior.

    Foreign keys are on. Contact, family, and organisation rows reference
    ``locations.id``; a stub ``locations`` table is created so that check has
    a target. ``location_id`` stays null, so geographic areas are not created.
    The JSONB sqlite compiler exists only while this session is open.
    """
    restore = _push_sqlite_jsonb_compiler()
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.create_function(
            "now",
            0,
            lambda: datetime.now(UTC).isoformat(),
        )
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE locations (id CHAR(32) NOT NULL PRIMARY KEY)"
            )
        Base.metadata.create_all(
            engine,
            tables=[
                Tag.__table__,
                Contact.__table__,
                ContactTag.__table__,
                Family.__table__,
                FamilyTag.__table__,
                Organization.__table__,
                OrganizationTag.__table__,
            ],
        )
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()
    finally:
        engine.dispose()
        restore()


def _tag(name: str) -> Tag:
    return Tag(id=uuid4(), name=name, created_by="test")


def _contact(first_name: str) -> Contact:
    return Contact(
        id=uuid4(),
        first_name=first_name,
        contact_type=ContactType.PARENT,
        relationship_type=RelationshipType.PROSPECT,
        source=ContactSource.CONTACT_FORM,
        mailchimp_status=MailchimpSyncStatus.PENDING,
    )


def test_replace_contact_tags_keeps_loaded_links_and_saves_parent() -> None:
    with _tag_session() as session:
        kept = _tag("kept")
        dropped = _tag("dropped")
        added = _tag("added")
        contact = _contact("Nora")
        session.add_all([kept, dropped, added, contact])
        session.flush()
        session.add_all(
            [
                ContactTag(contact_id=contact.id, tag_id=kept.id),
                ContactTag(contact_id=contact.id, tag_id=dropped.id),
            ]
        )
        session.commit()

        loaded = session.execute(
            select(Contact)
            .where(Contact.id == contact.id)
            .options(selectinload(Contact.contact_tags))
        ).scalar_one()
        replace_contact_tags(
            session,
            contact_id=loaded.id,
            tag_ids=[kept.id, added.id, kept.id],
        )
        loaded.last_name = "Bennett"
        session.add(loaded)
        session.commit()

        again = session.get(Contact, contact.id)
        assert again is not None
        assert again.last_name == "Bennett"
        assert {row.tag_id for row in again.contact_tags} == {kept.id, added.id}


def test_replace_contact_tags_deletes_links_when_collection_is_not_loaded() -> None:
    with _tag_session() as session:
        kept = _tag("kept")
        dropped = _tag("dropped")
        contact = _contact("Nora")
        session.add_all([kept, dropped, contact])
        session.flush()
        session.add_all(
            [
                ContactTag(contact_id=contact.id, tag_id=kept.id),
                ContactTag(contact_id=contact.id, tag_id=dropped.id),
            ]
        )
        session.flush()

        parent = session.get(Contact, contact.id)
        assert parent is not None
        assert "contact_tags" in instance_state(parent).unloaded

        replace_contact_tags(session, contact_id=parent.id, tag_ids=[kept.id])
        parent.last_name = "Bennett"
        session.add(parent)
        session.commit()

        again = session.get(Contact, contact.id)
        assert again is not None
        assert again.last_name == "Bennett"
        assert {row.tag_id for row in again.contact_tags} == {kept.id}


def test_replace_family_and_organization_tags_survive_parent_add() -> None:
    with _tag_session() as session:
        family_tag = _tag("family")
        family_dropped = _tag("family-dropped")
        org_tag = _tag("org")
        family = Family(
            id=uuid4(),
            family_name="North",
            relationship_type=RelationshipType.PROSPECT,
        )
        organization = Organization(
            id=uuid4(),
            name="Acme",
            organization_type=OrganizationType.OTHER,
            relationship_type=RelationshipType.PROSPECT,
        )
        session.add_all([family_tag, family_dropped, org_tag, family, organization])
        session.flush()
        session.add(FamilyTag(family_id=family.id, tag_id=family_tag.id))
        session.add(FamilyTag(family_id=family.id, tag_id=family_dropped.id))
        session.add(OrganizationTag(organization_id=organization.id, tag_id=org_tag.id))
        session.commit()

        loaded_family = session.execute(
            select(Family)
            .where(Family.id == family.id)
            .options(selectinload(Family.family_tags))
        ).scalar_one()
        replace_family_tags(
            session, family_id=loaded_family.id, tag_ids=[family_tag.id]
        )
        loaded_family.family_name = "Northwind"
        session.add(loaded_family)

        loaded_org = session.execute(
            select(Organization)
            .where(Organization.id == organization.id)
            .options(selectinload(Organization.organization_tags))
        ).scalar_one()
        replace_organization_tags(
            session, organization_id=loaded_org.id, tag_ids=[org_tag.id]
        )
        loaded_org.name = "Acme Co"
        session.add(loaded_org)
        session.commit()

        stored_family = session.get(Family, family.id)
        stored_org = session.get(Organization, organization.id)
        assert stored_family is not None
        assert stored_org is not None
        assert stored_family.family_name == "Northwind"
        assert {row.tag_id for row in stored_family.family_tags} == {family_tag.id}
        assert stored_org.name == "Acme Co"


def test_replace_contact_tags_rejects_archived_without_dropping_links() -> None:
    with _tag_session() as session:
        active = _tag("active")
        archived = _tag("archived")
        archived.archived_at = datetime.now(UTC)
        contact = _contact("Nora")
        session.add_all([active, archived, contact])
        session.flush()
        session.add(ContactTag(contact_id=contact.id, tag_id=active.id))
        session.commit()

        with pytest.raises(ValidationError, match="tag is archived"):
            replace_contact_tags(
                session,
                contact_id=contact.id,
                tag_ids=[active.id, archived.id],
            )
        session.rollback()
        reloaded = session.get(Contact, contact.id)
        assert reloaded is not None
        assert {row.tag_id for row in reloaded.contact_tags} == {active.id}


def test_tag_session_rejects_orphan_contact_tag() -> None:
    with _tag_session() as session:
        tag = _tag("only")
        session.add(tag)
        session.flush()
        session.add(ContactTag(contact_id=uuid4(), tag_id=tag.id))
        with pytest.raises(IntegrityError):
            session.flush()


def test_sqlite_jsonb_compiler_does_not_outlive_tag_session() -> None:
    from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

    with _tag_session() as session:
        assert session.get(Tag, uuid4()) is None
    with pytest.raises(UnsupportedCompilationError):
        JSONB().compile(dialect=sqlite_dialect())


def test_require_assignable_tag_raises_for_archived() -> None:
    tid = uuid4()
    session = MagicMock()
    session.get.return_value = SimpleNamespace(id=tid, archived_at="x")

    with pytest.raises(ValidationError, match="tag is archived"):
        require_assignable_tag(session, tid)
