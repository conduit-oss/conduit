# Wrapped client fixture

A small app whose own code calls a helper. The helper calls the SDK. `conduit map` follows that wrapper, aliases, and re-exports. `dynamic_gap.py` passes a variable to `getattr` and stays invisible.

```bash
conduit map --path examples/wrapped-openai-fixture --service openai
```
