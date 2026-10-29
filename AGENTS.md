# Maintenance

Read the public contract and its owners for the task:

| Task | Read |
|---|---|
| Change reviewer defaults, permissions, or skip detection | [review/README.md](review/README.md), [action.yml](review/action.yml), and the affected `review/*.mjs` files and tests. |
| Change comment verification or the failure marker | [verify-claude-review/README.md](verify-claude-review/README.md), its [action.yml](verify-claude-review/action.yml), script, and tests. |
| Change classification or diagnostics | [classify-claude-review/README.md](classify-claude-review/README.md), its [action.yml](classify-claude-review/action.yml), [vocabulary](classify-claude-review/failure-classes.json), script, and tests. |
| Change validation or runtime | [.repository.json](.repository.json), [.node-version](.node-version), [suite plan](.repository-suite-plan.json) and [dependency setup](scripts/setup-schema-tests.mjs), and [CI](.github/workflows/test.yml). |

Keep public inputs, outputs, and failure semantics beside each action.
Claude review is best-effort: a posted `claude[bot]` comment is success; red means no comment was posted.
Classification supplies diagnostics; `verify-claude-review` owns comment verification.

Use the Node and Python versions in `.node-version` and `.python-version`.
Install test-only schema dependencies with `python3 -m pip install --require-hashes -r requirements-schema-tests.txt`.
The capture action uses only the Python standard library.
Run `bash "$HOME/.agents/skills/repository/action.sh" run --repo "$PWD" --scope changed` through the shared repository tools.
Run full validation through the same shared command with `--scope full`.
For standalone native checks with the pinned runtimes, run `bash scripts/run-ci-checks.sh`.
Use `--only node-test`, `--only ci-run-context`, or `--only ci-run-context-schema` for focused reruns.
Changed validation selects units from declared inputs; shared tooling changes select all three. Public CI runs the three plan unit commands without access to the private toolkit.
The shared repository command owns toolkit selection.
The plan owns selection, filtering, and pass replay. Setup only creates the pinned Python environment and installs hash-locked test dependencies.

After a compatible change merges, release `v1` from the fetched merge commit: `git fetch origin main`, then `git tag -f v1 origin/main` and `git push -f origin v1`.
