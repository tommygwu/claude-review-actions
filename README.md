# claude-review-actions

Shared GitHub composite actions for [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action) PR reviews.

| Action | Public contract |
|---|---|
| [`review`](review/README.md) | Run the reviewer with shared model, tool permissions, and workflow-skip detection. |
| [`classify-claude-review`](classify-claude-review/README.md) | Classify execution evidence and emit a failure marker for caller diagnostics. |

Claude review is best-effort: a posted `claude[bot]` comment counts as success; red means no comment was posted.
The caller checks comment delivery; action outcomes and failure classes alone do not prove delivery.

`v1` tracks compatible releases; pin a full commit SHA to control updates.
For maintenance and validation, see [AGENTS.md](AGENTS.md).
