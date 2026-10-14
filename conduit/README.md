# Conduit package

Python CLI for outside services: `map`, `watch`, `impact`, `apply`, and `service`. Map, watch, and impact are Python only today. Other languages are planned one at a time. TypeScript and JavaScript via tree-sitter are next. Packet apply still contains language engines for frozen rules. See the [root README](../README.md).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[llm,langs,dev]"
conduit --help
```

The `langs` extra installs tree-sitter grammars for those packet-apply engines. The outside-service path does not use them yet. Without the extra, packet AST rules still run via regex and string fallbacks.
