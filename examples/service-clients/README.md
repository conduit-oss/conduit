# Service clients

A small repo that talks to four outside services:

- SQLAlchemy (`db.py`)
- S3 through boto3 (`storage.py`)
- a Stripe webhook (`webhooks.py`)
- a tiny recorded API client, `acme-sdk` (`acme_client.py`)

`unrelated.py` imports pydantic and requests. Those are not outside services.

`conduit map` walks wrappers, aliases, and re-exports. `conduit impact` diffs recorded wheels, the S3 botocore model, and the Stripe webhook schema. The version comes from `--from`, an exact pin, a lockfile, or the installed distribution. If none of those exist, the line is `not compared` and says why, instead of an all-clear.

```bash
conduit map --path examples/service-clients
conduit impact --dep sqlalchemy --path examples/service-clients --fixtures conduit/fixtures/impact
conduit impact --dep boto3 --from 1.34.0 --to 1.35.0 --path examples/service-clients --fixtures conduit/fixtures/impact
conduit watch --path examples/service-clients --feeds conduit/fixtures/feeds
```
