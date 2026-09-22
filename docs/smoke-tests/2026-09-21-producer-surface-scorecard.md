# Producer-surface scorecard (2026-09-21)

Catalog freeze for the pydantic 1.10.13 → 2.0.0 hop authored from surface snapshots
plus a reshape recipe. This is not a guide/LLM remint.

## Pipeline

```text
snapshot (surface packets)
  → diff (packet diff-surface + recipe)
  → migration hop freeze
  → apply / Watch on validator fixture
```

| Stage | Artifact |
|-------|----------|
| Snapshot v1 | `examples/surface-packets/pydantic-pypi-1.10.13.json` |
| Snapshot v2 | `examples/surface-packets/pydantic-pypi-2.0.0.json` |
| Recipe | `examples/reshape-recipes/pydantic-1.10.13-2.0.0.json` |
| Hop freeze (catalog name) | `examples/sample-packet/pydantic-surface-hop.json` |
| Draft alias | `examples/sample-packet/pydantic-surface-diff-draft.json` (identical bytes) |

Regenerate the hop:

```bash
PYTHONPATH=conduit/src python -m conduit.main packet diff-surface \
  --from examples/surface-packets/pydantic-pypi-1.10.13.json \
  --to examples/surface-packets/pydantic-pypi-2.0.0.json \
  --recipe examples/reshape-recipes/pydantic-1.10.13-2.0.0.json \
  --out examples/sample-packet/pydantic-surface-hop.json
```

Freeze SHA-256 (UTF-8, LF, trailing newline):
`c2fdb7a78a84042dc6af9ca9ac924631c78dbb0a1b41817887b0b801487271f5`

`validate_packet` returns `[]` on the freeze
(`conduit/tests/test_surface_hop_freeze.py`).

## Stack (merged onto main)

| PR | Branch | URL |
|----|--------|-----|
| #69 surface packet schema + snapshots | `stack/surface-packet-p1` | https://github.com/conduit-oss/conduit/pull/69 |
| #70 surface diff spike | `stack/surface-diff-p2` | https://github.com/conduit-oss/conduit/pull/70 |
| #71 surface diff onto main | `stack/surface-diff-onto-main` | https://github.com/conduit-oss/conduit/pull/71 |
| #72 declaration companions | `stack/surface-decl-companions` | https://github.com/conduit-oss/conduit/pull/72 |
| #73 reshape recipes | `stack/surface-reshape-recipes` | https://github.com/conduit-oss/conduit/pull/73 |

## Rule family counts (freeze)

| Family | Count |
|--------|------:|
| DEPENDENCY_BUMP | 1 |
| AST_CALL_REWRITE | 6 |
| AST_DECLARATION_REWRITE | 5 |
| rules total | 12 |
| side_effects | 2 |

Declaration op breakdown inside `AST_DECLARATION_REWRITE`:

| Op | Count |
|----|------:|
| import_member | 2 |
| ensure_classmethod | 2 |
| inner_class_to_assignment | 1 |

Call rewrites cover `BaseModel.dict` / `json` / `parse_obj` / `parse_raw`,
`root_validator`, and `validator`.

Structured gaps on the freeze:

| gap_kind | Detail |
|----------|--------|
| uncodable | Public export removed with no rename match: `MAX_EMAIL_LENGTH` |
| signature | `field_validator` may still need signature or `mode=` reshape beyond `@classmethod` |

## Gate vs completeness (validator fixture)

Evidence lives in `conduit/tests/test_surface_diff.py::test_pydantic_surface_diff_apply_fixture`.
That test runs surface hop + recipe through watch → apply → watch on
`examples/pydantic-validator-fixture`.

| Gate | Verdict | Evidence |
|------|---------|----------|
| Packet receipt (`validate_packet`) | PASS | freeze SHA above; pytest freeze check |
| Watch dirty → apply → Watch clean | PASS | fixture leftovers cleared for declared rules |
| Expressible hops mechanical (dict, validator+classmethod, Config) | PASS | call + declaration ops on freeze |
| Completeness for full pydantic v2 / mergeable consumer | FAIL | signature/`mode=` gap remains; OpenAPI and catalog publish not done |

### Dual verdict (operator)

| Gate | Verdict |
|------|---------|
| Gate validity (declared surfaces; fixture red → green) | PASS |
| Migration completeness (full producer surface / catalog) | FAIL |

## Remaining gaps

1. **signature / `mode=`** — structured `side_effects` gap; no declaration op yet for validator signature reshape.
2. **OpenAPI producer path** — REST surface mint/diff not landed as a catalog hop.
3. **Catalog publish** — hop freeze is in-repo only; not yet published as a catalog entry.

Hand-authored sibling for the same fixture story:
`examples/sample-packet/pydantic-validator-hop.json` (narrower rule set). Prefer
`pydantic-surface-hop.json` when the authoring path is producer surface + recipe.
