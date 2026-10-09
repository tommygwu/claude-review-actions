# review

Review prepared PR text without executing PR code. This action preserves Claude
Code subscription OAuth and comments authored by `claude[bot]`.

The trusted caller must run from main with a main-only credential environment.
It must not check out PR source, load a PR-local action, or run a PR command.
This action cannot protect a credential that the caller already exposed in an
untrusted job. Updating to this contract is a reviewed dependency change; the
old `prompt` and `allowed_tools` inputs are removed.

## Inputs and outputs

| Input | Meaning |
| --- | --- |
| `claude_code_oauth_token` | Existing subscription token from the trusted environment. |
| `pr_number` | PR selected by trusted workflow logic. |
| `reviewed_head_sha` | Exact lowercase 40-character PR head. |

The repository comes from `github.repository`. No caller prompt, settings, tools,
provider endpoint, CLI path, or prepared review data is accepted.

| Output | Meaning |
| --- | --- |
| `execution_file` | Version 1 typed envelope for the classifier. |
| `result_file` | The same terminal envelope for trusted downstream delivery. |
| `reviewed_head_sha` | The PR head under review. |
| `producer_sha` | Trusted workflow source, from `github.workflow_sha`. |
| `comment_id` | Verified Claude bot comment ID, only on `completed`. |
| `skip_reason` | `workflow-modified` if the Claude App refuses workflow validation. |

The envelope is `{version:1,repository,pr_number,workflow_run,attempt,failure_class,reviewed_head_sha,producer_sha}` with
`comment_id` on completion. `failure_class` uses
[the existing vocabulary](../classify-claude-review/failure-classes.json).
The producer source SHA is distinct from the reviewed PR SHA. Delivery consumers
must authenticate the producer run and check the current reviewed head.
`workflow_run` and `attempt` are integers that bind the artifact to its producing run.

## Boundary

Trusted Node code requests the Claude App token through GitHub OIDC, using the
same [audience and exchange endpoint as upstream](https://github.com/anthropics/claude-code-action/blob/2dca132ff0e0c4094ce6048b422c6915a071210b/src/github/token.ts).
It requests contents read and PR/issue write permissions. There is no workflow
token fallback. The token stays in the trusted parent and is revoked on exit.

The action checks the PR head before and after reading its title, body, and API
diff. It rejects unavailable, binary, oversized, or file-count-incomplete diffs.
It sends that envelope only as user-message data. It never reads PR files from
disk. Conservative rejection means a large or binary PR needs a human review.

URL user credentials and all URL query/fragment values in title, body, and diff
are replaced with markers before the model runs. This includes deletion lines
for historical credential URLs. The same redaction runs before posting review
text. Only token-free path context remains; hidden query values cannot be
reviewed by the model. Ambiguous URL encoding fails for human review without
logging the source value.

The CLI is pinned to 2.1.295 with platform archive SHA-512 integrity. It runs in
a new empty HOME and working directory outside the checkout, with a fixed
replacement system prompt. The child receives only its OAuth token and fixed
runtime controls; it receives no GitHub credentials, inherited environment,
provider URL, or runner settings. The model has no file, shell, browser, or MCP
tools. CLI process credentials are not model context.

The [official CLI reference](https://code.claude.com/docs/en/cli-reference)
documents the controls used here: safe mode, restricted mode, empty built-in
tools, denied MCP tools, strict empty MCP configuration, empty setting sources,
disabled skills, and a replacement system prompt. Bare mode is not used because
it does not support subscription OAuth. The fixture runs this exact pinned CLI
against a local fake provider and checks the actual request's empty tool list.
This is local transport proof, not hosted credential or live model proof.

Before posting, trusted code checks the head again, adds
`<!-- claude-review-run:RUN_ID:ATTEMPT:PR_HEAD -->`, verifies the returned author,
body, and comment ID, then checks the head once more. A changed or unverified
head cannot produce `completed`. The remaining read/post race does not make an
old-head comment valid for a new head; consumers must recheck the current head.

## Usage

Use a reviewed full SHA for `REVIEWED_ACTION_COMMIT` below. This placeholder must
be replaced before committing a caller.

```yaml
- id: review
  uses: tommygwu/claude-review-actions/review@REVIEWED_ACTION_COMMIT
  continue-on-error: true
  with:
    claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    pr_number: ${{ inputs.pr_number }}
    reviewed_head_sha: ${{ inputs.reviewed_head_sha }}
- id: classify
  if: ${{ !cancelled() }}
  uses: tommygwu/claude-review-actions/classify-claude-review@REVIEWED_ACTION_COMMIT
  with:
    execution_file: ${{ steps.review.outputs.execution_file }}
    action_outcome: ${{ steps.review.outcome }}
```

The job needs `id-token: write` and the Claude App installation. The wrapper has
no nested external Actions. CI checkout and Node setup are full-SHA pinned.

Live proof remains separate: after merge and the approved environment cutover,
verify denied same-repository PR credential access, exact-head evidence, and
actual authenticated receiver/monitor delivery. The orchestrator owns that check.
