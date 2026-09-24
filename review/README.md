# review

Composite action that runs [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action)
as a PR reviewer. It owns the settings every repository shares, so a model
change is one edit here plus a `v1` tag move.

## What this action owns

- The model: `--model claude-opus-5-5`.
- `--disallowed-tools "Agent,Task"`. Subagents run in the background in a
  headless runner and nothing re-invokes the parent, so a reviewer that ends its
  turn "waiting" posts nothing.

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
        if: ${{ !cancelled() && steps.claude-review.outcome != 'success' }}
        uses: tommygwu/claude-review-actions/classify-claude-review@v1
        with:
          execution_file: ${{ steps.claude-review.outputs.execution_file }}
          action_outcome: ${{ steps.claude-review.outcome }}
```

The job still needs the permissions `claude-code-action` requires, including
`id-token: write` and `pull-requests: write` to post the comment.

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
