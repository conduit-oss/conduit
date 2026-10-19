# apply-break

Tiny consumer with one SQLAlchemy `Connection.scalar` parameter rename
(`object_` → `statement`) from Conduit's wheel-diff evidence, plus recorded
`impact.json`.

```bash
export NEBIUS_API_KEY=...
conduit apply --path conduit/tests/fixtures/apply-break --impact conduit/tests/fixtures/apply-break/impact.json --write --fixtures conduit/fixtures/impact --test-cmd "python3 -m pytest -q"
```
