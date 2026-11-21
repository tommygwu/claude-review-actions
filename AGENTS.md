# Maintenance

[README.md](README.md) is the caller contract, and each `action.yml` owns its inputs and outputs.
Change them in the same commit as the behavior they describe; add no per-action README.

| Task | Read |
|---|---|
| Change reviewer defaults, permissions, or skip detection | The README `review` section, [action.yml](review/action.yml), and the affected `review/*.mjs` files and tests. |
| Change objective extraction | The README `objective-context` section, its [action.yml](objective-context/action.yml), module, and tests. |
| Change comment verification or the failure marker | The README `verify-claude-review` section, its [action.yml](verify-claude-review/action.yml), script, and tests. |
| Change classification or diagnostics | The README failure-classes section, the [vocabulary](classify-claude-review/failure-classes.json), module, and tests, plus the `verify-claude-review` annotations. |
| Change post-merge test cadence | The README `post-merge-cadence` section, its [action.yml](.github/actions/post-merge-cadence/action.yml), Python script, recorded fixtures, and tests, plus validation wiring. |
| Change the toolkit fetch | The README `agents-toolkit-fetch` section, its [action.yml](agents-toolkit-fetch/action.yml), `fetch.py`, and tests, plus the `toolkit-e2e` job. |
| Change the toolkit restore | The README `agents-toolkit` section, its [action.yml](agents-toolkit/action.yml), `restore.py`, and tests, plus the `toolkit-e2e` job. |
| Change the CI census | The README `agents-census` section, its [action.yml](agents-census/action.yml), `census.py`, and tests, plus the `toolkit-census` job. |
| Change the shared GitHub client | [lib/github_api.py](lib/github_api.py) and its tests, plus every caller: `cadence.py`, `fetch.py`, and `census.py`. |
| Change validation or runtime | [.repository.json](.repository.json), [.node-version](.node-version), [suite plan](.repository-suite-plan.json) and [dependency setup](scripts/setup-schema-tests.mjs), and [CI](.github/workflows/test.yml). |

The README's [best-effort review policy](README.md#best-effort-review-policy) is the only statement of success and red; link to it instead of restating it.
Classification supplies diagnostics; `verify-claude-review` owns comment verification.

Use the Node and Python versions in `.node-version` and `.python-version`.
Install test-only schema dependencies with `python3 -m pip install --require-hashes -r requirements-schema-tests.txt`.
The capture, cadence, toolkit fetch, toolkit restore, and census actions use only the Python standard library, and their programs stay compatible with Python 3.12 because fetch and restore run on the runner's own `python3`.
Run `bash "$HOME/.agents/skills/repository/action.sh" run --repo "$PWD" --scope changed` through the shared repository tools.
Run full validation through the same shared command with `--scope full`.
For standalone native checks with the pinned runtimes, run `bash scripts/run-ci-checks.sh`.
Use `--only node-test`, `--only ci-run-context`, `--only ci-run-context-schema`, `--only post-merge-cadence`, or `--only toolkit-actions` for focused reruns.
Changed validation selects units from declared inputs; shared tooling changes select all units.
Public CI runs the plan unit commands without access to the private toolkit; plan units stay keyless.
Only the `toolkit-e2e` and `toolkit-census` jobs hold the agents read key, and only on same-repository pull requests.
This repository is public, so no workflow here uploads agents content: anyone signed in to GitHub can download its artifacts.
The shared repository command owns toolkit selection.
The plan owns selection, filtering, and pass replay. Setup only creates the pinned Python environment and installs hash-locked test dependencies.

After a compatible change merges, release `v1` from the fetched merge commit: `git fetch origin main`.
Then list every `tommygwu/claude-review-actions/...@<sha>` pin on each registered consumer's default branch and check each with `git merge-base --is-ancestor <sha> origin/main`; keep that output as release proof.
Only then run `git tag -f v1 origin/main` and `git push -f origin v1`.
