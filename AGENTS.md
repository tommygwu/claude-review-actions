# Maintenance

Read the public contract and its owners for the task:

| Task | Read |
|---|---|
| Change reviewer defaults, permissions, or skip detection | [review/README.md](review/README.md), [action.yml](review/action.yml), and the affected `review/*.mjs` files and tests. |
| Change classification or diagnostics | [classify-claude-review/README.md](classify-claude-review/README.md), its [action.yml](classify-claude-review/action.yml), [vocabulary](classify-claude-review/failure-classes.json), script, and tests. |
| Change validation or runtime | [.repository.json](.repository.json), [.node-version](.node-version), [validator](scripts/validate.mjs), and [CI](.github/workflows/test.yml). |

Keep public inputs, outputs, and failure semantics beside each action.
Claude review is best-effort: a posted `claude[bot]` comment is success; red means no comment was posted.
Classification supplies diagnostics; callers own comment verification.

Use the Node and Python versions in `.node-version` and `.python-version`.
Install test-only schema dependencies with `python3 -m pip install --require-hashes -r requirements-schema-tests.txt`.
The capture action uses only the Python standard library.
Run `bash "$HOME/.agents/skills/repository/action.sh" run --repo "$PWD" --scope changed` through the shared repository tools.
For standalone validation with the pinned Node version, run `node scripts/validate.mjs full`.
Both validation scopes run all action tests; CI runs `full`.

After a compatible change merges, release `v1` from the fetched merge commit: `git fetch origin main`, then `git tag -f v1 origin/main` and `git push -f origin v1`.
