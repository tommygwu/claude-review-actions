# verify-claude-review

Composite action that owns the whole post-review sequence: verify, classify, keep the transcript, and report.
Claude review is best-effort: a `claude[bot]` comment created in this run's window is success; red means no comment was posted.

## Usage

```yaml
      - name: Record review start time
        id: review-start
        run: echo "ts=$(date -u +%s)" >> "$GITHUB_OUTPUT"

      - name: Run Claude Code Review
        id: claude-review
        continue-on-error: true
        uses: tommygwu/claude-review-actions/review@v1
        with:
          # ...

      - name: Verify a review comment was posted
        if: ${{ !cancelled() }}
        uses: tommygwu/claude-review-actions/verify-claude-review@v1
        with:
          execution_file: ${{ steps.claude-review.outputs.execution_file }}
          review_outcome: ${{ steps.claude-review.outcome }}
          run_marker: '<!-- claude-review-run:${{ github.run_id }}:${{ github.run_attempt }}:${{ github.event.pull_request.head.sha }} -->'
          start_ts: ${{ steps.review-start.outputs.ts }}
          head_sha: ${{ github.event.pull_request.head.sha }}
          github_token: ${{ secrets.GITHUB_TOKEN }}
```

Keep the job name `claude-review` and the step name `Verify a review comment was posted`.
`gh run view --log-failed` attributes a composite action's output to the calling step, and the claude-review-verdict consumer trusts only that job and step.
Never set `continue-on-error` on this step: it turns the step green and drops the marker from `--log-failed`.
Give every later step in the job `continue-on-error: true`, so only this step can turn a posted review red.
`continue-on-error` on the review step is required, so the review's `outcome` stays raw and this step owns the job's only red condition.

## Inputs and output

| Input | Required | Description |
|---|---|---|
| `execution_file` | no | `steps.<review-step-id>.outputs.execution_file`; empty when upstream wrote none. |
| `review_outcome` | yes | `steps.<review-step-id>.outcome`. |
| `run_marker` | yes | The marker the prompt asks the reviewer to end its comment with; a missing marker only warns. |
| `start_ts` | yes | Epoch seconds recorded before the review step. |
| `head_sha` | yes | The reviewed head SHA. |
| `github_token` | yes | A token that can list the PR's issue comments. |
| `pr_number` | no | Defaults to `github.event.pull_request.number`. |
| `expect_comment` | no | `"false"` for a dry run that renders instead of posting; the action then passes without verifying. Default `"true"`. |

The output `class` is empty when a comment was verified; otherwise it holds the failure class.

## Decision

| Evidence | Class | Check |
|---|---|---|
| A `claude[bot]` comment at or after `start_ts` | none | green, whatever `review_outcome` is |
| No comment, `review_outcome=success` | `never-posted` | red |
| No comment, any other outcome | the [classifier](../classify-claude-review/README.md#classification-rules) class | red |
| `start_ts` not an integer, `review_outcome=success` | `unknown` | red |
| `start_ts` not an integer, any other outcome | the classifier class, without counting comments | red |
| Comments unreadable or unparseable | `unknown` | red |

A repository that also runs `claude.yml`, the `@claude` responder that posts as `claude[bot]`, can pass with an in-window `@claude` reply and no review; the action shows this as the missing-marker warning, and the best-effort policy accepts it.

On red, the action uploads `execution_file` as the `claude-review-execution-<head>-<attempt>` artifact, emits a class annotation, and prints `CLAUDE_REVIEW_FAILURE_CLASS=<class>` as its last line.
