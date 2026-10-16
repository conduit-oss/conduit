"""SQLAlchemy call sites. Query.from_self is removed in the 2.0 library."""

from sqlalchemy.orm import Query, Session


class User:
    id = 0


def by_pk(user_id: int):
    query = Query(User)
    return query.from_self(user_id)


def load_user(user_id: int):
    session = Session()
    return session.query(User)
