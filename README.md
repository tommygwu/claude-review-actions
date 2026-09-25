# claude-review-actions

Shared GitHub composite actions for [`claude-code-action`](https://github.com/anthropics/claude-code-action)
PR-review workflows.

| Action | Purpose |
|---|---|
| [`review`](./review) | Run `claude-code-action` as a PR reviewer with the fleet's shared model and review defaults. |
| [`classify-claude-review`](./classify-claude-review) | Derive a closed-vocabulary failure class from the review action's `execution_file`, so a red check says *why* it is red. |

Each action directory carries its own README, and any tests or data artifacts it
publishes as a contract.

`v1` moves as compatible changes ship. After a change merges, move the tag to
the merge commit: `git tag -f v1 origin/main && git push -f origin v1`.

## Verifying

```bash
node --test 'classify-claude-review/*.test.mjs' 'review/*.test.mjs'
```
