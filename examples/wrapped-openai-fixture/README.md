# Wrapped OpenAI fixture

A small consumer that never calls `openai.ChatCompletion.create` from application code. The legacy SDK call sits behind a function, a class method, and a package re-export. `alias_client.py` constructs `OpenAI` under the alias `C`. `http_edge.py` posts to `api.openai.com` with `requests`. `dynamic_gap.py` uses `getattr` and is a known miss.

From the repo root:

```bash
export PYTHONPATH=conduit/src
python -m conduit.main map \
  --path examples/wrapped-openai-fixture \
  --service openai \
  --packet examples/sample-packet/conduit-packet.json
```

`--json` prints the same map for Watch and apply. The 0.28 pin makes `conduit watch` a pre-bump warn; the wrapped call sites are still listed.
