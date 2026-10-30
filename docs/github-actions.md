# GitHub Actions

Two different things live under this name.

The composite action in [`conduit/action.yml`](../conduit/action.yml) installs Conduit and runs `map`, or `watch` when you set `mode` to `watch`. It does not call Nebius Token Factory and it does not comment on a pull request.

The bump workflow below is the one that rewrites call sites and posts one comment. It is the workflow in [council-test `.github/workflows/conduit.yml`](https://github.com/conduit-oss/council-test/blob/main/.github/workflows/conduit.yml). It installs the Conduit CLI from a pinned commit and runs `impact`, `baseline`, and `apply`. It never pushes and it never merges.

## Workflows in this repo

These jobs call the same commands as the README. They do not run `apply` and they do not comment.

| File | What it does |
|------|----------------|
| [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | Tests, then an offline map of `examples/wrapped-openai-fixture`. |
| [`.github/workflows/conduit-watch-demo.yml`](../.github/workflows/conduit-watch-demo.yml) | `conduit map` and `conduit watch` on recorded copies. |
| [`.github/workflows/conduit-dependabot.yml`](../.github/workflows/conduit-dependabot.yml) | `conduit map` when a pull request touches a dependency file. |
| [`.github/workflows/conduit-nightly.yml`](../.github/workflows/conduit-nightly.yml) | `conduit map` on a schedule. |

A minimal job that only maps the repo:

```yaml
- uses: actions/checkout@v4
- uses: actions/setup-python@v5
  with:
    python-version: "3.11"
- run: pip install -e ./conduit
- run: conduit map --path .
```

## 1. Add `NEBIUS_API_KEY`

`conduit apply` reads the Token Factory key from the environment variable `NEBIUS_API_KEY`. The workflow must pass it in. Never print the value and never commit it.

On GitHub, open the repository that will run the workflow:

1. **Settings**
2. **Secrets and variables**
3. **Actions**
4. **New repository secret**
5. Name: `NEBIUS_API_KEY`. Value: the Token Factory key. Save.

The workflow refers to it as `secrets.NEBIUS_API_KEY`.

Dependabot pull requests cannot read Actions secrets. GitHub fills `secrets.*` from **Dependabot secrets** when `github.actor` is `dependabot[bot]`. Add the same name under **Settings → Secrets and variables → Dependabot**. Renovate and other pull requests use the Actions secret.

The composite action does not forward `NEBIUS_API_KEY`. Setting the secret does nothing for a job that only `uses` the action.

## 2. The composite action

`conduit/action.yml` inputs, and what the current steps do with each one:

| Input | Default | Used by the steps |
|-------|---------|-------------------|
| `path` | `.` | Passed to `conduit map` or `conduit watch` as `--path`. |
| `mode` | `run` | `watch` runs `conduit watch`. Any other value runs `conduit map`. |
| `python-version` | `3.11` | Passed to `actions/setup-python`. |
| `base-ref` | `""` | Declared. The steps do not read it. |
| `package` | `""` | Declared. The steps do not read it. |
| `packet` | `""` | Declared. The steps do not read it. |
| `skip-pr` | `"false"` | Declared. The steps do not read it. |

There is no input for the API key, the model, or a test command. Do not add `with:` keys that are not in this table.

The action's run step copies `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CONDUIT_LLM_PROVIDER`, `CONDUIT_LLM_MODEL`, `CONDUIT_LLM_API_KEY`, `CONDUIT_LLM_BASE_URL`, `GH_TOKEN`, and `GITHUB_TOKEN` from the job environment when those are set. `map` and `watch` do not call Token Factory. The step does not copy `NEBIUS_API_KEY`.

Pinned use (the action file is in the `conduit/` directory):

```yaml
- uses: conduit-oss/conduit/conduit@761c055d313e2ac88881372b3509b1f6163dd9db
  with:
    path: .
    mode: watch
    python-version: "3.11"
```

`761c055d313e2ac88881372b3509b1f6163dd9db` is `main` at the time of this page. Pin a full commit. A moving branch name will not stay on the Conduit you tested.

That step checks out nothing by itself. Put `actions/checkout` before it. `mode: watch` with no `--feeds` tries the network. For a read-only gate inside this repository, the [watch demo workflow](../.github/workflows/conduit-watch-demo.yml) passes recorded feeds on the CLI instead.

## 3. Bump pull requests

### When it runs

The workflow triggers on `pull_request` when one of these files changes:

- `requirements*.txt`
- `dev-requirements*.txt`
- `pyproject.toml`

A Dependabot or Renovate bump that edits one of those files starts the job. The workflow does not filter on `github.actor`. The detect step only treats a change as a bump when a `*requirements*.txt` pin (`==` or `~=`) changes. A pull request that only edits `pyproject.toml` can start the job and then print `no pinned dependency changed` and skip `apply`.

[`.github/workflows/conduit-dependabot.yml`](../.github/workflows/conduit-dependabot.yml) is a different job. It only runs `conduit map`, and only when the actor is `dependabot[bot]` or `renovate[bot]`, or the pull request has the label `conduit`.

### Permissions

The comment job sets:

```yaml
permissions:
  contents: read
  pull-requests: write
```

`contents: read` is enough to check out the pull request. The job never pushes, so it does not need `contents: write`.

`pull-requests: write` is what the comment step uses. The script calls the issue-comment API on the pull request (`listComments`, `createComment`, `updateComment`). That is the permission block on the council-test workflow that posted the demo comment.

Dependabot-triggered runs start with a read-only `GITHUB_TOKEN`. The `permissions` block above is what raises it enough to comment. The key still has to be a Dependabot secret, as in step 1.

### The workflow file

Save this as `.github/workflows/conduit.yml` in the repository that receives the bump. It follows the council-test file. The install pin is current `main` (`761c055d313e2ac88881372b3509b1f6163dd9db`). The demo run [40790485322](https://github.com/conduit-oss/council-test/actions/runs/40790485322) installed `192c68f80d0b0a5ab13c9aa58ddbb2a6e7131201`.

`TEST_CMD`, `requirements.txt`, and `pytest==7.4.4` are that demo repo (`tests/unit` on a fork of council). Point them at your own test command and requirement files. Conduit's flags in the script are real CLI flags: `impact --dep --from --to --json`, `baseline --test-cmd --ref --out`, `apply --impact --write --test-cmd --baseline --json`.

```yaml
# On a PR that bumps a pinned dependency: find the call sites, ask Nemotron
# on Nebius Token Factory for fixes, verify them with the repo tests, and
# comment once. Never pushes and never merges.
name: Conduit

on:
  pull_request:
    paths:
      - "requirements*.txt"
      - "dev-requirements*.txt"
      - "pyproject.toml"

permissions:
  contents: read
  pull-requests: write

env:
  CONDUIT_SPEC: "git+https://github.com/conduit-oss/conduit.git@761c055d313e2ac88881372b3509b1f6163dd9db#subdirectory=conduit"
  CONDUIT_APPLY_MODEL: nvidia/Nemotron-3-Ultra-550b-a55b
  CONDUIT_APPLY_ENABLE_THINKING: "false"
  TEST_CMD: python -m pytest -q tests/unit

jobs:
  conduit:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout PR head
        uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Check NEBIUS_API_KEY
        id: secret
        env:
          NEBIUS_API_KEY: ${{ secrets.NEBIUS_API_KEY }}
        run: |
          if [ -z "${NEBIUS_API_KEY}" ]; then echo "present=false" >> "$GITHUB_OUTPUT"; else echo "present=true" >> "$GITHUB_OUTPUT"; fi

      - name: Detect bumped pins from the PR diff
        if: steps.secret.outputs.present == 'true'
        id: detect
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: |
          python - <<'PY'
          import json, os, pathlib, re, subprocess
          pin = re.compile(r"(?mi)^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*(?:==|~=)\s*([^\s#;,\\]+)")
          base = os.environ["BASE_SHA"]
          def pins(text):
              return {m.group(1).lower().replace("_", "-"): (m.group(1), m.group(2)) for m in pin.finditer(text)}
          files = subprocess.check_output(["git", "diff", "--name-only", f"{base}...HEAD", "--", "*requirements*.txt"], text=True).split()
          changed = {}
          for name in files:
              try:
                  before = subprocess.check_output(["git", "show", f"{base}:{name}"], text=True)
              except subprocess.CalledProcessError:
                  before = ""
              after = pathlib.Path(name).read_text(encoding="utf-8") if pathlib.Path(name).exists() else ""
              old, new = pins(before), pins(after)
              for key, (dist, to_ver) in new.items():
                  if key in old and old[key][1] != to_ver:
                      changed[key] = [dist, old[key][1], to_ver]
          pathlib.Path("/tmp/changed.json").write_text(json.dumps(list(changed.values())))
          summary = "; ".join(f"{d} {a} -> {b}" for d, a, b in changed.values())
          with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
              fh.write(f"summary={summary}\n")
              fh.write(f"any={'true' if changed else 'false'}\n")
          print(summary or "no pinned dependency changed")
          PY

      - name: Install BASE pins and Conduit (impact needs the old release installed)
        if: steps.detect.outputs.any == 'true'
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: |
          python -m pip install -U pip
          git show "${BASE_SHA}:requirements.txt" > /tmp/base-requirements.txt
          pip install -r /tmp/base-requirements.txt pytest==7.4.4
          pip install "${CONDUIT_SPEC}"

      - name: Conduit impact (base pins installed)
        if: steps.detect.outputs.any == 'true'
        run: |
          python - <<'PY'
          import json, pathlib, subprocess
          merged = {"impacts": [], "deprecations": [], "diffed": [], "notes": []}
          for dist, old, new in json.loads(pathlib.Path("/tmp/changed.json").read_text()):
              out = subprocess.check_output(["conduit", "impact", "--dep", dist, "--from", old, "--to", new, "--json"], text=True)
              payload = json.loads(out)
              for key in merged:
                  merged[key].extend(payload.get(key) or [])
          merged["dependency"] = ",".join(d for d, _, _ in json.loads(pathlib.Path("/tmp/changed.json").read_text()))
          pathlib.Path("impact.json").write_text(json.dumps(merged, indent=2))
          print(sum(len(i.get("sites") or []) for i in merged["impacts"]), "impact sites")
          PY
          mv impact.json /tmp/impact.json

      - name: Conduit baseline (base pins, base code)
        if: steps.detect.outputs.any == 'true'
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: conduit baseline --test-cmd "${TEST_CMD}" --ref "${BASE_SHA}" --out /tmp/baseline.json

      - name: Install PR pins
        if: steps.detect.outputs.any == 'true'
        run: pip install -r requirements.txt

      - name: Conduit apply (Ultra, thinking off; verifies with tests)
        if: steps.detect.outputs.any == 'true'
        id: apply
        env:
          NEBIUS_API_KEY: ${{ secrets.NEBIUS_API_KEY }}
        run: |
          set +e
          # httpx logs each Token Factory request at INFO (URL and status only, never the key).
          python - --impact /tmp/impact.json --write --test-cmd "${TEST_CMD}" --baseline /tmp/baseline.json --json > /tmp/apply.json 2> /tmp/apply.log <<'PY'
          import logging, sys
          logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
          logging.getLogger("httpx").setLevel(logging.INFO)
          from conduit.main import app
          sys.argv = ["conduit", "apply", *sys.argv[1:]]
          app()
          PY
          echo "exit=$?" >> "$GITHUB_OUTPUT"
          git diff -- '*.py' > /tmp/apply.diff
          grep -m1 "HTTP Request: POST .*chat/completions" /tmp/apply.log > /tmp/tf-call.txt || true
          ${TEST_CMD} > /tmp/tests-after.txt 2>&1; echo "tests_after_exit=$?" >> /tmp/tests-after.txt
          tail -5 /tmp/apply.log

      - name: Comment on the PR
        if: always() && steps.secret.outputs.present == 'true'
        uses: actions/github-script@v7
        env:
          SUMMARY: ${{ steps.detect.outputs.summary }}
        with:
          script: |
            const fs = require('fs');
            const read = (p) => { try { return fs.readFileSync(p, 'utf8'); } catch (e) { return ''; } };
            const json = (p) => { try { return JSON.parse(read(p)); } catch (e) { return {}; } };
            const marker = '<!-- conduit -->';
            const impact = json('/tmp/impact.json');
            const apply = json('/tmp/apply.json');
            const sites = [];
            for (const item of impact.impacts || []) for (const s of item.sites || [])
              sites.push(`| ${s.path}:${s.line} | \`${(s.expr || '').replace(/\|/g, '\\|')}\` | ${item.change || ''}${s.via_wrapper ? ' (via wrapper)' : ''} |`);
            const results = (apply.results || []).map(r =>
              `| ${r.path}:${r.line} | ${r.status} | ${(r.why || r.error || '').split('\n')[0].replace(/\|/g, '\\|')} |`);
            const diff = read('/tmp/apply.diff').trim();
            const tests = read('/tmp/tests-after.txt').trim().split('\n').slice(-3).join('\n');
            const body = [
              marker,
              '## Conduit',
              '',
              `Bump: \`${process.env.SUMMARY || 'none detected'}\`. Model: \`${process.env.CONDUIT_APPLY_MODEL}\` (Nebius Token Factory, enable_thinking=false). Tokens: ${apply.tokens_used ?? 'n/a'}.`,
              '',
              'Token Factory call (from the run log):',
              '```', read('/tmp/tf-call.txt').trim() || '(no model call logged)', '```',
              '',
              '### Impact', '', '| Site | Expression | Change |', '| --- | --- | --- |', ...(sites.length ? sites : ['| _none_ | | |']),
              '',
              '### Apply', '', '| Site | Status | Why |', '| --- | --- | --- |', ...(results.length ? results : ['| _none_ | | |']),
              '',
              `### Tests after apply (\`${process.env.TEST_CMD}\`)`, '```', tests || '(not run)', '```',
              ...(apply.preexisting_note ? ['', `_${apply.preexisting_note}_`] : []),
              '',
              '### Proposed diff', diff ? '```diff\n' + diff.slice(0, 60000) + '\n```' : '_No diff._',
              '',
              '_Conduit only comments: it never pushes to this branch and never merges._',
            ].join('\n');
            const { owner, repo } = context.repo;
            const issue_number = context.issue.number;
            const { data: comments } = await github.rest.issues.listComments({ owner, repo, issue_number, per_page: 100 });
            const existing = comments.find(c => c.body && c.body.includes(marker));
            if (existing) await github.rest.issues.updateComment({ owner, repo, comment_id: existing.id, body });
            else await github.rest.issues.createComment({ owner, repo, issue_number, body });
```

`--write` writes inside the runner's checkout so `git diff` can capture the patch. Nothing in the file runs `git push` or a merge.

`CONDUIT_APPLY_MODEL` selects the rewrite model. The one-line why uses the default small model, `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, unless you set `CONDUIT_APPLY_MODEL_SMALL`. `CONDUIT_APPLY_ENABLE_THINKING=false` matches the default (`chat_template_kwargs.enable_thinking` false). The small model defaults to thinking off as well.

## 4. The pull request comment

The comment step posts one comment. A later run on the same pull request updates that comment. The hidden marker is `<!-- conduit -->`.

The body looks like this:

```text
## Conduit

Bump: `<package> <old> -> <new>`. Model: `nvidia/Nemotron-3-Ultra-550b-a55b` (Nebius Token Factory, enable_thinking=false). Tokens: <tokens_used>.

Token Factory call (from the run log):
    the first httpx line that records POST .../chat/completions
    (URL and status only)

### Impact
| Site | Expression | Change |

### Apply
| Site | Status | Why |

### Tests after apply (`python -m pytest -q tests/unit`)
    the last three lines of the test command

### Proposed diff
    the git diff of *.py, or "No diff."

Conduit only comments: it never pushes to this branch and never merges.
```

On the demo run the bump line is `anthropic 0.34.2 -> 1.0.0`, and `Tokens` is the run total `26250`.

`Status` is the string `conduit apply` writes. `Why` is one sentence.

| Status | What it means |
|--------|----------------|
| `fixed (verified)` | The rewrite is kept. The test command passed, and a test ran the changed lines. The rewrite did not drop a value the old call passed. |
| `fixed, not covered by tests` | The rewrite is kept and the suite did not gain a new failure, but no test ran the changed lines. This is not "verified". |
| `fixed, tests couldn't run` | The rewrite is kept, but the suite could not collect or run after the bump (import/collection errors, including tests that still import or mock a removed API). Distinct from "not covered": the tests were broken, not merely silent about this line. Never "verified". |
| `fixed, needs review` | The rewrite is kept and the tests passed, but the new call drops a value the old call passed. The why starts with `drops ...` (the demo case drops `settings`). This is never "verified". |
| `needs-human` | No rewrite was kept. The why is one sentence: the tests still failed, a check still failed, a site this one depends on was not fixed, or the line could not be rewritten. `conduit apply` exits 1 when any site is needs-human, unless you passed `--dry-run`. The workflow still comments. |
| `skipped` | The model was not asked. The line is the same function as another site (`same function as path:line; covered by that rewrite and its recheck`), a wrapper caller (`wrapper caller; fixed at the SDK line`), or the line was already rewritten (`line rewritten by an earlier fix in this file`). |
| `fixed (unverified: no tests)` | `--test-cmd` was omitted, so the suite did not run. The bump workflow always sets `TEST_CMD`, so this status does not come from that file. |
| `dry-run` | `--dry-run`. The model was not called. |

**Already failing on base** is not a site status. Before the new pins are installed, `conduit baseline` runs the same test command on the base commit and records failures. After the rewrite, a failure that was already in that file is not blamed on the fix. The comment prints the note in italics under the test output, for example `4 tests already failing on base, not caused by Conduit: ...`. The demo suite was 320 tests and had 0 new failures (the same 4 already failing).

## 5. Token Factory lines in the log

`conduit apply` prints usage to stderr, after the report. The endpoint, model, thinking flag, call count, and token count are included. The API key is not.

```text
Token Factory call: POST https://api.tokenfactory.nebius.com/v1/chat/completions model=nvidia/Nemotron-3-Ultra-550b-a55b thinking=off calls=10 tokens=24782
Token Factory call: POST https://api.tokenfactory.nebius.com/v1/chat/completions model=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B thinking=off calls=5 tokens=1468
Token Factory total: calls=15 tokens=26250
```

Those three lines are from the demo run. The first model writes the rewrites. The second writes the one-line why. `thinking=off` means `enable_thinking=false`.

The workflow turns httpx request logging on and tails the last five lines of `/tmp/apply.log`, so the job log shows the same `Token Factory call` lines. The comment's "Token Factory call" block is a different line: the first `HTTP Request: POST .../chat/completions` record. That line is the URL and the HTTP status. It is not the token total. The token total in the comment header is `tokens_used` from the apply JSON, which matches `Token Factory total`.

## 6. Troubleshooting

### The key is missing

The check step writes `present=false` and does not print the key. Every later step is skipped, including the comment. The job is green and the pull request has no Conduit comment.

Locally, `conduit apply` stops with: `NEBIUS_API_KEY is not set. Export it to call Token Factory, or pass a fake client in tests.`

Check the secret name, which repository you saved it on, and, for a Dependabot pull request, that a Dependabot secret with the same name exists. Re-running a Dependabot-triggered run still cannot see Actions secrets.

### The repo's tests cannot be installed

`Install BASE pins and Conduit` installs the base `requirements.txt` plus the demo's `pytest==7.4.4`, then Conduit. `Install PR pins` installs the pull request's `requirements.txt`. If either `pip install` fails, `apply` does not run.

The job log stops on that install step (a resolution error, a build error, or a missing file). The comment step still runs when the key was present, because its condition is `always()`. The apply table is empty (`_none_`) because `/tmp/apply.json` was never written. Fix the requirement files or the Python version and re-run the job.

If pip succeeds and the test command fails (pytest is missing, or collection fails), apply treats that as a failed suite. Rewrites that cannot be cleared come back as `needs-human` with a why such as `Reverted: the tests failed with this rewrite.`

### The Python version does not match

Conduit requires Python 3.10 or newer (`requires-python` in `conduit/pyproject.toml`). The composite action's `python-version` input and this workflow's `actions/setup-python` step both default to `3.11`.

If the repository's tests need another version, change `python-version` on `actions/setup-python` in the workflow (or the action input, if you are only mapping). A mismatch shows up as pip failing to install a package, or as pytest failing to collect, on the install or test step. Set the version the repo's suite uses, then re-run.
