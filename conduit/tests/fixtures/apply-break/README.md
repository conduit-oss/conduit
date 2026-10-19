# apply-break

Tiny consumer with one SQLAlchemy `Query.from_self` break and a recorded
`impact.json` for live `conduit apply --write` smoke tests.

```bash
export NEBIUS_API_KEY=...
conduit apply --path conduit/tests/fixtures/apply-break --impact conduit/tests/fixtures/apply-break/impact.json --write --fixtures conduit/fixtures/impact
```
