"""SQLAlchemy call sites. The 2.0 surface drops Query.get and deprecates Session.query."""

from sqlalchemy.orm import Query, Session


class User:
    id = 0


def by_pk(user_id: int):
    query = Query()
    return query.get(user_id)


def load_user(user_id: int):
    session = Session()
    return session.query(User).get(user_id)
