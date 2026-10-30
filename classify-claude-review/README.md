# classify-claude-review

Classifier module that derives one failure class from the review step's outcome and execution evidence, without a model call or prose matching.
It is not an action: [`verify-claude-review`](../verify-claude-review/README.md) imports it, runs it only when no comment was posted, and owns the annotation and the failure marker.
A failure class supplies diagnostics and does not prove whether a comment exists.

Classes use the closed vocabulary in [failure-classes.json](failure-classes.json).
An unreadable execution file other than a missing one gives `unknown` for a non-success outcome.

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
| `never-posted` | Emitted by `verify-claude-review` for a successful review step with no comment; the classifier never emits it. |

`startup-failure` does not prove the reviewer never started.
Early usage exhaustion, cancellation, a failed file write, and startup faults can leave the same evidence.
The classifier cannot recover a cause that is absent from its inputs; check the review step's log and error annotation before spending a retry.

## Skip marker

The [review action](../review/README.md#workflow-skip-detection) owns workflow comparison and writes `$RUNNER_TEMP/claude-review-skip.json`.
The classifier reads it without another input.
For non-success outcomes, a `workflow-modified` marker applies only to missing or empty execution output; a non-empty execution file takes priority.
A `not-executed` marker keeps `startup-failure`.
Direct upstream calls do not produce this marker, so a successful upstream skip is classified as `completed` and still needs caller comment verification.

Tests check the classifier, skip-marker integration, and vocabulary parity.
See [maintenance validation](../AGENTS.md) for the declared command.
