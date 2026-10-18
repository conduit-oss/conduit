# Service clients

A small app used by the README:

- SQLAlchemy (`db.py`)
- S3 through boto3 (`storage.py`)
- a Stripe webhook (`webhooks.py`)

`unrelated.py` imports pydantic and requests. Those are not outside services.

```bash
conduit map --path examples/service-clients
conduit impact --dep sqlalchemy --path examples/service-clients --fixtures conduit/fixtures/impact
conduit watch --path examples/service-clients --feeds conduit/fixtures/feeds
```

The offline copies are recordings of public releases. Rebuild them with `python scripts/record_fixtures.py`.
