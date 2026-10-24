# classify-claude-review

Composite action that classifies the review step's outcome and execution evidence without a model call or prose matching.
The caller owns comment verification: a posted `claude[bot]` comment counts as success; red means no comment was posted.
A failure class supplies diagnostics and does not prove whether a comment exists.

## Usage

For the paired reviewer and classifier steps, see the [review example](../review/README.md#usage).
To classify a direct `anthropics/claude-code-action` call, use its step ID in both inputs:

```yaml
      - name: Classify review outcome
        id: classify
        if: ${{ !cancelled() }}
        continue-on-error: true
        uses: tommygwu/claude-review-actions/classify-claude-review@v1
        with:
          execution_file: ${{ steps.claude-review.outputs.execution_file }}
          action_outcome: ${{ steps.claude-review.outcome }}
```

Run classification with `if: ${{ !cancelled() }}` so it also handles a successful review step.
`completed` means the step outcome was `success`, even with no execution file; it does not prove the reviewer ran or posted.
An empty `class` after classification was expected means the output was not produced; inspect step execution and wiring.
`continue-on-error` keeps diagnostics from preventing the caller's comment check.

## Inputs and output

| Input | Required | Description |
|---|---|---|
| `execution_file` | yes | `steps.<review-step-id>.outputs.execution_file`; the path can be empty when upstream produced no file. |
| `action_outcome` | yes | `steps.<review-step-id>.outcome`: `success`, `failure`, or `cancelled`. |

The output `class` uses the closed vocabulary in [failure-classes.json](failure-classes.json).
The action prints `CLAUDE_REVIEW_FAILURE_CLASS=<class>`, emits a class annotation, and exits nonzero for every class other than `completed`.
An interpreter failure falls back to `unknown`.

## Classification rules

For a successful step outcome, the result is `completed` regardless of execution-file contents.
For other outcomes, the classifier applies these rules:

| Class | Evidence and handling |
|---|---|
| `auth-failed` | Typed assistant authentication error or non-empty `auth_status.error`; repair credentials before retrying. Takes priority over usage evidence. |
| `out-of-usage` | Typed assistant `rate_limit` or `billing_error`, or a rejected `rate_limit_event`; wait for usage to reset. Takes priority over work recorded. |
| `workflow-modified` | Missing or empty execution output and a `workflow-modified` skip marker; reruns do not repair the workflow mismatch. |
| `reviewer-errored` | A result message records turns or cost without an auth or usage signal; a retry may help. |
| `startup-failure` | Missing or empty output, an empty/non-array message value, or no result and no typed auth or usage signal; inspect the review log before retrying. |
| `unknown` | Invalid JSON, an unreadable file other than a missing file, or a result with no turns, cost, auth signal, or usage signal. |
| `never-posted` | Reserved for caller comment verification; this action never emits it. |

`startup-failure` does not prove the reviewer never started.
Early usage exhaustion, cancellation, a failed file write, and startup faults can leave the same evidence.
The action cannot recover a cause that is absent from its inputs; check the review step's log and error annotation before spending a retry.

## Skip marker

The [review action](../review/README.md#workflow-skip-detection) owns workflow comparison and writes `$RUNNER_TEMP/claude-review-skip.json`.
The classifier reads it without another input.
For non-success outcomes, a `workflow-modified` marker applies only to missing or empty execution output; a non-empty execution file takes priority.
A `not-executed` marker keeps `startup-failure`.
Direct upstream calls do not produce this marker, so a successful upstream skip is classified as `completed` and still needs caller comment verification.

## Log-marker consumers

Consumers that parse `gh run view --log-failed` by the tab-separated prefix `claude-review\tClassify review outcome\t` require the job name `claude-review` and calling step name `Classify review outcome`.
Composite-action log lines use the calling step name.
Keep those names stable when a consumer uses that parser; a renamed step can still emit a marker the parser cannot find.
With `continue-on-error`, the classifier step may be absent from `--log-failed`; stable names alone do not ensure marker visibility.
Use `steps.classify.outputs.class` within the job, or inspect full logs when needed.

Tests check the classifier, action wiring, skip-marker integration, and vocabulary parity.
See [maintenance validation](../AGENTS.md) for the declared command.
