# review

Composite action that runs [`anthropics/claude-code-action@v1`](https://github.com/anthropics/claude-code-action) for PR reviews.
It selects `--model claude-opus-5-5` and disables `Agent,Task` tools because background subagents can leave the headless reviewer waiting without posting.

## Usage

```yaml
      - name: Run Claude Code Review
        id: claude-review
        continue-on-error: true
        uses: tommygwu/claude-review-actions/review@v1
        with:
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          review_file: review.md
          allowed_tools: "Bash(gh pr diff:*),Bash(gh pr view:*),Bash(gh pr comment:*),Bash(cat:*)"
          prompt: |
            Review PR ${{ github.event.pull_request.number }} in ${{ github.repository }}.
            Write the review to review.md and post it as a PR comment.

      - name: Classify review outcome
        id: classify
        if: ${{ !cancelled() }}
        continue-on-error: true
        uses: tommygwu/claude-review-actions/classify-claude-review@v1
        with:
          execution_file: ${{ steps.claude-review.outputs.execution_file }}
          action_outcome: ${{ steps.claude-review.outcome }}
```

These steps run the reviewer and collect diagnostics; the caller then checks whether a `claude[bot]` comment was posted.
Claude review is best-effort: that comment counts as success; red means no comment was posted.
`continue-on-error` lets the caller reach comment verification after an action failure; use `outcome` for classification because `conclusion` can become `success`.
For classifier conditions, failure classes, and log-marker names, see its [public contract](../classify-claude-review/README.md).

The caller owns the prompt, token, additional tools, step environment, and comment verification.
With `review_file`, start the action at `GITHUB_WORKSPACE` and use that same root filename in the prompt.
Grant the permissions required by upstream, plus `contents: read` for workflow-skip detection.

## Inputs

| Input | Required | Description |
|---|---|---|
| `claude_code_oauth_token` | yes | Claude Code OAuth token. |
| `prompt` | yes | Complete review prompt. |
| `allowed_tools` | yes | Comma-separated permission rules passed as `--allowed-tools`, in addition to upstream's base tools; one line with no double quotes or CR/LF. |
| `review_file` | no | Root filename that starts with a letter or digit and contains only letters, digits, `.`, `_`, or `-`; empty adds no file or permission. |

`review_file` adds `Edit(/<filename>)`, including Write access, at the initial directory.
The action creates a missing file with mode `0600`, preserves an existing regular file, and rejects a directory or symlink before Claude starts.

## Outputs

| Output | Description |
|---|---|
| `execution_file` | Upstream execution-file path, passed to the classifier. |
| `skip_reason` | `workflow-modified` or `not-executed` when the skip check runs; otherwise empty. |

## Workflow-skip detection

The skip check runs after an upstream success with neither `conclusion` nor `execution_file`.
It compares the running workflow blob at `github.workflow_sha` (or `github.sha`) with the default branch.
It reads the running blob locally, with a GitHub API fallback, and always reads the default-branch blob through the API to avoid a stale local ref.

| `skip_reason` | Detection rule |
|---|---|
| `workflow-modified` | Non-empty prompt and a different or unverified workflow blob. This represents the upstream workflow-validation skip; reruns do not repair a workflow mismatch. |
| `not-executed` | Empty prompt or identical workflow blobs. Read the upstream step log for the cause. |

Either result writes `$RUNNER_TEMP/claude-review-skip.json`, sets `skip_reason`, prints an error annotation, and fails the action.
The classifier reads that marker when execution output is missing or empty.
An unverified comparison is an inference from the upstream output shape, not a confirmed file difference.
