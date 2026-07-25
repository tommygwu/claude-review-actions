# classify-claude-review

Composite action that derives one closed-vocabulary **failure class** from the
[`claude-code-action`](https://github.com/anthropics/claude-code-action)
`execution_file` output, so a red review check says *why* it is red.

A review check that just says "failed" forces a human to open the log every
time. This turns the failure into a machine-readable class, so downstream
automation can tell "the provider refused for usage limits" (a rerun hits the
same wall) apart from "the reviewer errored mid-run" (a rerun probably works).

## Usage

```yaml
jobs:
  claude-review:            # job named exactly `claude-review`
    steps:
      # ... your anthropics/claude-code-action review step (id: claude-review) ...

      - name: Classify review outcome   # step named EXACTLY this
        id: classify
        if: ${{ !cancelled() }}
        uses: tommygwu/claude-review-actions/classify-claude-review@v1
        with:
          execution_file: ${{ steps.claude-review.outputs.execution_file }}
          action_outcome: ${{ steps.claude-review.outcome }}
```

`v1` moves as compatible changes ship. Pin a full commit SHA instead if you
want changes to reach your repo only when you deliberately bump.

## Inputs

| Input | Required | Description |
|---|---|---|
| `execution_file` | yes | `steps.<review-step-id>.outputs.execution_file` from the `anthropics/claude-code-action` step. |
| `action_outcome` | yes | `steps.<review-step-id>.outcome` — `success` \| `failure` \| `cancelled`. |

## Output

| Output | Description |
|---|---|
| `class` | The derived failure class, one of the values in [`failure-classes.json`](./failure-classes.json). |

## Failure classes

The complete closed vocabulary lives in [`failure-classes.json`](./failure-classes.json),
which this repo's own tests assert the classifier against:

- `completed` — the reviewer finished; the action exits `0` and comment
  verification follows.
- `out-of-usage` — the provider rejected the run for usage limits. **Terminal:**
  a rerun hits the same wall until usage resets.
- `auth-failed` — provider authentication failed. **Terminal:** repair the
  credential; reruns cannot fix a broken one.
- `reviewer-errored` — the reviewer ran (real turns and cost recorded) but
  errored before finishing. **Transient:** a rerun should succeed.
- `startup-failure` — no execution output at all. **Not proof the reviewer never
  started:** usage exhaustion before a usage signal, a cancelled step, or a
  failed execution-file write produce the same shape. Distinct from `unknown`,
  which means output *was* written but could not be read.
  See [Why `startup-failure` is ambiguous](#why-startup-failure-is-ambiguous).
- `unknown` — no classifiable evidence (e.g. an unparseable execution file).
- `never-posted` — **workflow-derived, not emitted by this action.** The
  caller's verify/fallback steps emit it when a `completed` run left no valid
  review comment. The classifier never returns it.

Classification reads typed fields from the SDK message array — never prose, and
never a model call. `auth-failed` outranks `out-of-usage` because a broken
credential is deterministic across reruns.

### Why `startup-failure` is ambiguous

`out-of-usage` is derived from typed evidence *inside* the execution file. When
usage exhaustion kills the run before that evidence is recorded, the class
degrades to `startup-failure`, and no amount of logic here can recover it:

- This action's only inputs are `execution_file` and `action_outcome`. On this
  path `action_outcome` is `failure` for every cause.
- `claude-code-action` writes the execution file with whatever messages
  accumulated — from its SDK catch handler on a thrown request, and from the
  post-loop path when the stream ends with no `result`. So a first-request
  refusal produces a literal `[]`, or an init-only stream, which is exactly what
  a non-usage startup error (bad model, network failure, malformed prompt)
  produces.
- The provider's actual refusal reason survives only as prose: the *review*
  step's job log (`SDK execution error: …`) and that step's `::error::`
  annotation (`Action failed with error: …`). Neither is an action output, and
  neither is readable by a later step in the same job.

So the honest handling is to say so: the `startup-failure` annotation names the
causes and points the operator at the review step's log and annotation, rather
than asserting a startup problem that may not exist.

**Known consequence, not yet solved.** `startup-failure` is rerun-eligible
downstream — correct for a genuine startup problem. But a usage-exhausted run
that lands here is not simply charged one wasted rerun: consumers that requeue
after a usage reset key on the `out-of-usage` verdict, so this run falls outside
that sweep and stays red with no automated path back. Closing that gap is a
consumer-side change, not a producer one; it is tracked separately.

For every non-`completed` class the action prints a per-class `::error::`
annotation and **exits non-zero**, so the check fails and the marker lands in
`gh run view --log-failed`.

## Marker transport invariant (read before adopting)

The action prints `CLAUDE_REVIEW_FAILURE_CLASS=<class>`. Tooling that reads the
class back out of `gh run view --log-failed` matches on the tab-separated
`<job>\tClassify review outcome\t` prefix, and `--log-failed` attributes every
line a composite action prints to the **calling step's name**, not the action's
internal step name.

Two names in the adopting workflow are therefore load-bearing:

- the **job** must be named `claude-review`;
- the calling **step** must be named exactly `Classify review outcome`.

Rename either and the marker still prints, but the consumer regex stops
matching — a silent failure. Pin both in a test if you depend on them.

## Consuming the class downstream

`steps.classify.outputs.class` is available to later steps. A common pattern is
a `Verify a review comment was posted` step that folds `completed` →
`never-posted` when the reviewer finished but posted nothing usable. That
folding, and any self-heal built on it, stays in the calling workflow — this
action only produces the class.

If your workflow branches on individual class names, pin the set you depend on
in your own test. This repo's `failure-classes.json` is not reachable from a
consumer's test run, so nothing will otherwise tell you when the vocabulary
changes here.

## Verifying a change

```bash
node --test 'classify-claude-review/*.test.mjs'
```

The suite proves the classification logic and asserts `failure-classes.json`
stays in lockstep with the classifier's `FAILURE_CLASSES`.
