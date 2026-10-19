"""One param-rename break from Conduit wheel-diff evidence."""

from sqlalchemy.engine import Connection


def fetch_one(conn: Connection, stmt):
    return conn.scalar(object_=stmt)
