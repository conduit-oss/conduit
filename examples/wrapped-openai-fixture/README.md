# Wrapped OpenAI fixture

A small consumer that never calls `openai.ChatCompletion.create` from application code. `conduit map` follows the function wrapper, the class method, the package re-export, and the `OpenAI as C` alias. `http_edge.py` posts to `api.openai.com` with `requests`. `dynamic_gap.py` passes a variable to `getattr` and stays unresolved. A string literal passed to `getattr` is tracked. Python only.

From the repo root:

```bash
export PYTHONPATH=conduit/src
python -m conduit.main map \
  --path examples/wrapped-openai-fixture \
  --service openai \
  --packet examples/sample-packet/conduit-packet.json
```

`--json` prints the same map for Watch. `conduit watch --packet` against the sample packet is the leftover gate: the 0.28 pin is a pre-bump warn, and the wrapped call sites are still listed. That gate is not an internal-repo watch. `conduit apply` is deferred. Feed watch for this fixture reads recorded release notes. The OpenAI wheel pair used to check public names is built in tests and is not in `conduit/fixtures/impact`.
