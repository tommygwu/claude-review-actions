# Post-merge test cadence

Use `decide` in `post-merge-config` and gate every push test job on its `run_ci` output.
Use `check` from an hourly workflow to rerun the newest untested default-branch push in `interval` mode.
Reruns use GitHub's run rerun endpoint, retain event `push`, and start attempt 2 or later.
The action uses Python's standard library, Git, and GitHub records; it creates no persistent cadence state.

| Input | Contract |
|---|---|
| `operation` | `decide` (default) or `check`. |
| `post-merge-ci` | Required `true` or `false`, case-insensitive, for push decisions and checks; non-push decisions ignore it. |
| `cadence` | Explicit `every-merge`, `newest-merge`, or `interval`; every push decision and enabled check rejects an unset or unknown value and names all allowed values. |
| `interval-hours` | Positive whole number for interval pushes and enabled interval checks; ignored otherwise. |
| `test-workflows` | Exact workflow names, one per line; required for enabled `interval` checks, which evaluate each independently; `decide` uses the current workflow. |
| `non-test-jobs` | Exact job names, one per line, excluded from coverage; `post-merge-config` is always excluded. |
| `github-token` | Pass `${{ github.token }}` with `contents: read` for commit history and `checks: read` for batched check records; `decide` also requires `actions: read`, and `check` requires `actions: write`. |

`decide` outputs `run_ci=true` for every non-push event, including pull requests and manual runs, before reading post-merge settings.
Push decisions validate cadence and any interval hours even when `POST_MERGE_CI=false`, then output `false` for a valid disabled configuration.
Enabled push attempts in `every-merge` and `newest-merge` output `true`.
In `interval`, push attempt 1 outputs `false` and reports that the commit is not yet tested; attempts 2+ output `true`.
`POST_MERGE_CI=false` prevents push tests and push reruns; a disabled `check` reports no action before cadence validation.
Callers must run full scope on push so tests on a later commit cover skipped ancestor commits.

The caller's concurrency group supplies queuing: use a unique group per run for `every-merge`, a branch group for `newest-merge` retaining the caller's rerun-attempt term, and the same branch group across all attempts for `interval`.
Only pull requests should cancel an active run.
Keep the hourly checker in its own concurrency group with no overlapping checks; cadence does not change pull request or manual test workflow queuing.

`check` writes one summary line per workflow, including each no-action reason.
It does nothing when CI is off, the cadence is not `interval`, there is no push run, a push run remains active or queued, or the newest commit is covered.
Otherwise it reruns the newest push when no test exists, the interval since the last tested attempt's start has elapsed, or the last tested attempt failed.
A cancelled latest rerun does not erase an earlier valid tested attempt.
A replacement merge is reevaluated at the next check.

A tested attempt has a started, non-skipped job outside `non-test-jobs` in an attempt that was not cancelled.
An in-progress test supplies coverage while it runs; it supplies no final result yet.
Coverage is per workflow and follows commit ancestry, including earlier skipped commits covered by a later full-scope test.
Cancelled attempts supply no coverage.

On interval push attempt 1, `decide` fails when an earlier uncovered commit has waited strictly more than three intervals.
It excludes the current push and preserves the earliest arrival of each uncovered commit across later merges.
The first merge after a quiet period therefore passes.
Malformed settings, an omitted interval-check workflow list, missing or ambiguous workflow names, incomplete history, and API errors fail the action instead of assuming coverage.
Enabled interval checks first read one newest push candidate.
A completed, non-cancelled candidate skips full history only after its actual checks and attempts prove coverage, its run still matches, and a second newest-push read matches the original snapshot.
This covered-head path reads no older workflow history and fetches no commit graph.
An uncertain candidate or failed fast-path read uses the complete-history path.
Filtered run lists are read in created-at windows beyond GitHub's 1,000-result cap.
GraphQL batches paginate every available check run in each needed suite across attempts; incomplete or mismatched records fail instead of supplying negative evidence.
The latest tested tip's complete ancestry rules out covered older runs before more check-suite or attempt reads; histories without tests are still read completely.
Single-attempt checks must match the run and attempt identity; changed attempts and needed positive reruns use exact REST attempt/job records.
Coverage uses one temporary bare Git repository per invocation, fetching only verified tested tips with `--filter=tree:0` and no shallow limit.
Git commit ancestry includes divergent tested tips and stays separate for each workflow, even when object storage is shared.
The temporary repository is deleted on exit; credentials stay in process environment, outside URLs, command arguments, and files.
Git must be installed; a failed fetch or incomplete graph fails the action.

```yaml
permissions:
  contents: read
  checks: read
  actions: read
steps:
  - uses: tommygwu/claude-review-actions/.github/actions/post-merge-cadence@v1
    id: cadence
    with:
      operation: decide
      post-merge-ci: ${{ vars.POST_MERGE_CI }}
      cadence: ${{ vars.POST_MERGE_CADENCE }}
      interval-hours: ${{ vars.POST_MERGE_INTERVAL_HOURS }}
      github-token: ${{ github.token }}
```

For `check`, give the job `contents: read`, `checks: read`, and `actions: write`, set `operation: check`, and supply every test workflow in `test-workflows`.
The consumer owns the hourly schedule, workflow concurrency, full-scope tests, and all push-job output gates.
After a compatible merge, release `v1` from the fetched merge commit using the steps in the repository's `AGENTS.md`.
