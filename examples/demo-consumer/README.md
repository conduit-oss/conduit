# Demo consumer

Tiny Python app stuck on the **openai 0.28** SDK surface (`openai.ChatCompletion.create`, pin `openai==0.28.1`) so Conduit can prove a structural 0.28 → 1.x apply.

From the repo root (with the Conduit venv activated):

```bash
# repo root
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e "./conduit[langs,dev]"

conduit run --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json \
  --demo \
  --skip-tests \
  --skip-pr
```

After a successful run you should see `ChatCompletion.create` gone, an `OpenAI` client call chain, and the dependency pin bumped past 0.28. Restore with `git -C examples/demo-consumer checkout -- .` when you are done inspecting.
