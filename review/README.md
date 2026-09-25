# review

Composite action that runs [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action)
as a PR reviewer. It owns the settings every repository shares, so a model
change is one edit here plus a `v1` tag move.

## What this action owns

- The model: `--model claude-opus-5-5`.
- `--disallowed-tools "Agent,Task"`. Subagents run in the background in a
  headless runner and nothing re-invokes the parent, so a reviewer that ends its
  turn "waiting" posts nothing.
- Workflow-validation skip detection. See
  [When the PR modifies the review workflow](#when-the-pr-modifies-the-review-workflow).

## What the caller owns

- The prompt. Review focus, output format, and the run marker are specific to
  each repository.
- The tool allowlist. Repositories grant different `gh`, shell, and `Edit(path)`
  rules, and some compute them per run (for example, a dry-run that omits
  `gh pr comment`).
- The token, because a composite action cannot read secrets.
- Step `env:`. Variables set on the calling step reach the reviewer's shell, so
  a prompt can refer to them.
- Step-level `continue-on-error`, the classifier, and comment verification.
  With `continue-on-error`, read `steps.<id>.outcome` (it is `failure` on a
  detected skip), not `conclusion`.

## Usage

```yaml
      - name: Run Claude Code Review
        id: claude-review
        uses: tommygwu/claude-review-actions/review@v1
        with:
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          allowed_tools: "Bash(gh pr diff:*),Bash(gh pr view:*),Bash(gh pr comment:*),Bash(cat:*),Edit(review.md)"
          prompt: |
            REPO: ${{ github.repository }}
            PR NUMBER: ${{ github.event.pull_request.number }}
            ...

      - name: Classify review outcome
        id: classify
        if: ${{ !cancelled() }}
        uses: tommygwu/claude-review-actions/classify-claude-review@v1
        with:
          execution_file: ${{ steps.claude-review.outputs.execution_file }}
          action_outcome: ${{ steps.claude-review.outcome }}
```

Run classify with `if: ${{ !cancelled() }}`, not only when the review step
failed. It returns `completed` for a successful review, so its `class` output is
always set, and an empty class then really means the classify wiring is broken.
A condition on the review outcome makes an empty class ambiguous: it can also
mean classify was skipped.

The job still needs the permissions `claude-code-action` requires, including
`id-token: write` and `pull-requests: write` to post the comment, plus
`contents: read`, which the skip detection uses to read the workflow file.

## When the PR modifies the review workflow

`claude-code-action` refuses to run when the running workflow file differs from
the copy on the default branch. It logs "Workflow validation failed", skips the
review, and **exits success** with no execution file. It records the skip in an
inner output that its root action does not expose, so without help the skip is
invisible: the review step is green and nothing is posted.

This action detects the skip in a step after the review:

1. **Gate.** The step runs only when the review succeeded but set neither
   `conclusion` nor `execution_file`. Every real run sets `conclusion`, so the
   step never runs on a normal review. With a non-empty prompt, the only
   upstream path to this state is the validation skip.
2. **Confirm.** It takes the running workflow path from `github.workflow_ref`
   and compares the file's blob at `github.workflow_sha` with the blob on the
   default branch. It reads the running blob from the local checkout first and
   falls back to the GitHub contents API when the checkout is shallow or absent.
   The default branch always goes through the API, because a local
   remote-tracking ref can be stale.
3. **Report.** It sets `skip_reason`, writes a marker to
   `$RUNNER_TEMP/claude-review-skip.json`, prints an `::error::`, and exits
   non-zero, so the step outcome is `failure` and a
   `steps.claude-review.outcome != 'success'` condition fires.

| `skip_reason` | Meaning |
|---|---|
| `workflow-modified` | The workflow file differs from the default branch, or the diff could not be read (the gate already proves the skip). By design: reruns cannot fix it. The review runs on the next PR after this one merges; a maintainer reviews the workflow change by hand. `classify-claude-review` reads the marker and returns `workflow-modified`. |
| `not-executed` | The action returned success without running, but the workflow file matches the default branch (or the prompt was empty). Cause unknown. `classify-claude-review` returns `startup-failure`. |

## Inputs

| Input | Required | Description |
|---|---|---|
| `claude_code_oauth_token` | yes | `secrets.CLAUDE_CODE_OAUTH_TOKEN`. |
| `prompt` | yes | The complete review prompt. |
| `allowed_tools` | yes | Comma-separated permission rules passed as `--allowed-tools`. They add to the action's read-only base tool set. Must not contain a double quote. |

## Output

| Output | Description |
|---|---|
| `execution_file` | The `claude-code-action` execution file, for `classify-claude-review`. |
| `skip_reason` | `workflow-modified` or `not-executed` when the reviewer did not run; empty on every normal run. |
