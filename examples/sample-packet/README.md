# Conduit Migration Packet: openai-0.28.1-1.0.0

Structural **openai 0.28 → 1.x** kill-bar packet (`ChatCompletion.create` → modern chat completions + dependency bump). Consumers:

```bash
conduit run --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json \
  --demo \
  --skip-tests \
  --skip-pr
```
