# google_genai detect module

This module is **profile-driven**. OpenAI is the reference (`conduit.detect.modules.openai.profile.OPENAI_PROFILE`).

## What was generated

- `profile.py` — URLs, scan patterns, evidence hosts
- `run()` calls `run_profile_module` with `stock_workers_for(PROFILE)`
  - OpenAPI diff if `openapi_repo` is set
  - Live catalog polling if `live_catalog_url` is set
  - SDK release stepping if `sdk_release_repos` is set
  - OpenAI HTML deprecation/changelog scrapers are **not** enabled (`parser_kind=generic`)

## Checklist

1. Confirm links in `profile.py`. Empty strings should be `None` if unused.
2. **Custom parsers:** deprecation pages and changelogs almost always need a vendor-specific worker. Add it under `workers/` and include it in `run()`'s worker list. Do not invent path/call successors.
3. Fill `path_to_callees` / `api_pattern_to_path` if you want param-rename `function_target`s.
4. Add offline fixtures under `fixtures/google_genai/` (openapi/, deprecations/, models/, sdk_releases/).
5. Register built-in in `conduit.detect.modules.discovery._builtin_modules`, **or** for an external package:

```toml
[project.entry-points."conduit.detect_modules"]
google_genai = "your_package.module:GoogleGenaiModule"
```

6. Verify: `conduit module list` then `conduit detect --module google_genai --demo`
