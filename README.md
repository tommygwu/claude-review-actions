# claude-review-actions

Shared GitHub composite actions for [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action) PR reviews and CI cadence.

| Action | Public contract |
|---|---|
| [`review`](review/README.md) | Run the reviewer with shared model, tool permissions, and workflow-skip detection. |
| [`objective-context`](objective-context/README.md) | Extract acceptance criteria and non-goals from the PR body for the reviewer. |
| [`verify-claude-review`](verify-claude-review/README.md) | Verify the review comment, classify a silent run, keep its transcript, and emit the failure marker. |
| [`post-merge-cadence`](.github/actions/post-merge-cadence/README.md) | Decide when post-merge tests run and rerun the newest untested push when due. |

See the verifier’s [best-effort review policy](verify-claude-review/README.md#best-effort-review-policy).
`verify-claude-review` checks comment delivery; action outcomes and failure classes alone do not prove delivery.
Its [classifier module](classify-claude-review/README.md) owns the failure-class vocabulary.

`v1` tracks compatible releases; pin a full commit SHA to control updates.
For maintenance and validation, see [AGENTS.md](AGENTS.md).
