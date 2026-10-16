# Service clients

A small app used by the README:

- SQLAlchemy (`db.py`)
- S3 through boto3 (`storage.py`)
- a Stripe webhook (`webhooks.py`)
- a tiny API client, `acme-sdk` (`acme_client.py`)

`unrelated.py` imports pydantic and requests. Those are not outside services.

```bash
conduit map --path examples/service-clients
conduit impact --dep sqlalchemy --path examples/service-clients --fixtures conduit/fixtures/impact
conduit watch --path examples/service-clients --feeds conduit/fixtures/feeds
```
