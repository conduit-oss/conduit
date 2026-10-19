"""One breaking SQLAlchemy call site for apply smoke tests."""

from sqlalchemy.orm import Query


class User:
    id = 0


def by_pk(user_id: int):
    query = Query(User)
    return query.from_self(user_id)
