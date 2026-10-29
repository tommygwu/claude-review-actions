# classify-claude-review

Composite action that classifies the review step's outcome and execution evidence without a model call or prose matching.
[`verify-claude-review`](../verify-claude-review/README.md) runs this classifier only when no comment was posted after a failed review step, and owns the failure marker.
A failure class supplies diagnostics and does not prove whether a comment exists.

## Usage

Review workflows call [`verify-claude-review`](../verify-claude-review/README.md#usage), which imports this classifier.
The standalone action stays only until every review workflow calls `verify-claude-review`; add no new caller.

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
| `never-posted` | Emitted by `verify-claude-review` for a successful review step with no comment; this action never emits it. |

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

The claude-review-verdict consumer reads the marker that `verify-claude-review` prints under its calling step; see its [usage](../verify-claude-review/README.md#usage) for the required names.

Tests check the classifier, action wiring, skip-marker integration, and vocabulary parity.
See [maintenance validation](../AGENTS.md) for the declared command.
