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
| Change validation or runtime | [.repository.json](.repository.json), [.node-version](.node-version), [suite plan](.repository-suite-plan.json) and [dependency setup](scripts/setup-schema-tests.mjs), and [CI](.github/workflows/test.yml). |

The README's [best-effort review policy](README.md#best-effort-review-policy) is the only statement of success and red; link to it instead of restating it.
Classification supplies diagnostics; `verify-claude-review` owns comment verification.

Use the Node and Python versions in `.node-version` and `.python-version`.
Install test-only schema dependencies with `python3 -m pip install --require-hashes -r requirements-schema-tests.txt`.
The capture and cadence actions use only the Python standard library.
Run `bash "$HOME/.agents/skills/repository/action.sh" run --repo "$PWD" --scope changed` through the shared repository tools.
Run full validation through the same shared command with `--scope full`.
For standalone native checks with the pinned runtimes, run `bash scripts/run-ci-checks.sh`.
Use `--only node-test`, `--only ci-run-context`, `--only ci-run-context-schema`, or `--only post-merge-cadence` for focused reruns.
Changed validation selects units from declared inputs; shared tooling changes select all units.
Public CI runs the plan unit commands without access to the private toolkit.
The shared repository command owns toolkit selection.
The plan owns selection, filtering, and pass replay. Setup only creates the pinned Python environment and installs hash-locked test dependencies.

After a compatible change merges, release `v1` from the fetched merge commit: `git fetch origin main`, then `git tag -f v1 origin/main` and `git push -f origin v1`.
