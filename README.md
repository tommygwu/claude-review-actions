# claude-review-actions

Shared GitHub composite actions for [`claude-code-action`](https://github.com/anthropics/claude-code-action)
PR-review workflows.

| Action | Purpose |
|---|---|
| [`classify-claude-review`](./classify-claude-review) | Derive a closed-vocabulary failure class from the review action's `execution_file`, so a red check says *why* it is red. |

Each action directory carries its own README, tests, and any data artifacts it
publishes as a contract.

## Verifying

```bash
node --test 'classify-claude-review/*.test.mjs'
```
