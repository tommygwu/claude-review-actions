# claude-review-actions

Shared actions for Claude pull request reviews.

| Action | Purpose |
| --- | --- |
| [review](./review) | Read an exact PR head as text, run a tool-free reviewer, and post with the Claude App. |
| [classify-claude-review](./classify-claude-review) | Report the existing closed failure vocabulary from legacy execution files or version 1 results. |

Pin every caller to a reviewed full commit SHA. Adopt updates through dependency
PRs that review the action, CLI archive integrity pins, and the full executable
action graph. Do not move a shared tag as an adoption method.

Node 24.21.0 is declared in `.node-version`. The actions use Node built-ins;
there are no npm dependencies. The review tests download the exact CLI archive
from `review/claude-code-lock.json`, check its integrity, and use fake credentials
with a local provider fixture. They do not call a live model or GitHub.

Run the declared changed-scope gate with `BASE_REV=origin/main node scripts/checks.mjs changed`.
`.repository-suite-plan.json` owns the test commands; `.repository.json` owns the
changed-path selection. CI uses the same vectors. A main push runs full scope.

When this declaration first ships, the shared registry can still say `none`.
That publication receipt proves publication only. Record the direct changed
gate, its exact candidate SHA, and the required review separately. Promote the
registry declaration only after this declaration merges.
