# classify-claude-review

Report a closed failure class from `execution_file`. Version 1 text-review
results and legacy Claude Code SDK arrays use the same
[failure vocabulary](./failure-classes.json).

```yaml
- name: Classify review outcome
  id: classify
  if: ${{ !cancelled() }}
  uses: tommygwu/claude-review-actions/classify-claude-review@REVIEWED_ACTION_COMMIT
  with:
    execution_file: ${{ steps.review.outputs.execution_file }}
    action_outcome: ${{ steps.review.outcome }}
```

Replace `REVIEWED_ACTION_COMMIT` with a reviewed full commit SHA. Adopt updates
through dependency PRs. Run the classifier for both successful and failed
reviews so an empty class means missing wiring, rather than a skipped step.

| Input | Meaning |
| --- | --- |
| `execution_file` | File returned by the review step. |
| `action_outcome` | Step outcome: success, failure, or cancelled. |

The `class` output is always set when classification runs. Every class except
`completed` fails the step and prints `CLAUDE_REVIEW_FAILURE_CLASS=<class>`.

| Class | Meaning |
| --- | --- |
| `completed` | The text reviewer and trusted poster completed. Legacy success still needs caller comment verification. |
| `out-of-usage` | Typed provider usage refusal; wait for usage reset. |
| `auth-failed` | Provider or App authentication failed; repair the credential or authentication path. |
| `workflow-modified` | The Claude App refused workflow validation. A rerun cannot change the workflow source. |
| `reviewer-errored` | The review pipeline failed. Retry a transient error; source-coverage refusals need a human review. |
| `never-posted` | The trusted poster or legacy caller could not verify a Claude bot comment for the current head. |
| `startup-failure` | No classifiable execution data. This does not prove that the process never started. |
| `unknown` | Invalid, unreadable, unsupported, or unclassified evidence. |

Version 1 envelopes carry `failure_class` as typed data. Their
`reviewed_head_sha` is the PR source; `producer_sha` is the trusted workflow
source. A failed step cannot promote a typed completed result to success.
Unknown envelope versions and classes return `unknown`.

Legacy SDK arrays classify typed authentication and usage signals before result
shape. Authentication takes priority over usage. No model call or prose match
is used to derive the class. A legacy provider refusal that produces no typed
signal still reads as `startup-failure`; its old review log is the only evidence
for the underlying cause.

The old pinned wrapper can write `claude-review-skip.json` under `RUNNER_TEMP`.
This compatibility path returns `workflow-modified` only when no execution file
exists and the marker names that reason. A real execution file wins. The new
wrapper reports App validation refusal in its typed envelope directly.

Consumers reading the marker from `gh run view --log-failed` depend on the job
name `claude-review` and calling step name `Classify review outcome`. Preserve
those names. Consumers using result artifacts must also authenticate the run,
match its repository, PR, run ID and attempt, and verify the current reviewed
head. The classifier does not authenticate artifact provenance.

Run the focused suite with `node scripts/test-unit.mjs classifier`.
