#!/usr/bin/env node

import { readFileSync } from "node:fs";
import process from "node:process";
import { pathToFileURL } from "node:url";

// Claude Code Review failure classes, derived from the structured SDK message
// array the claude-code-action writes to its execution_file output. Downstream
// automation (github-fix) keys rerun decisions on this closed vocabulary, so
// classification reads typed fields — never prose, never a model call.
export const FAILURE_CLASSES = Object.freeze([
  "completed",
  "out-of-usage",
  "auth-failed",
  "reviewer-errored",
  // Workflow-derived only: the verify/fallback steps emit never-posted when a
  // completed run left no valid comment. classifyMessages never returns it.
  "never-posted",
  "startup-failure",
  "unknown",
]);

// SDKAssistantMessage.error values that mean the provider refused for usage.
// The review workflow authenticates with a subscription OAuth token, so usage
// exhaustion can also surface as a rate_limit_event with status "rejected".
const USAGE_ASSISTANT_ERRORS = Object.freeze(["rate_limit", "billing_error"]);

function isRecord(value) {
  return typeof value === "object" && value !== null;
}

/** @internal Exported for testing failure-path diagnostics. */
export function getErrorDetails(error) {
  const details = isRecord(error) ? error : null;
  return {
    code: typeof details?.code === "string" ? details.code : "unknown",
    message: typeof details?.message === "string" ? details.message : String(error),
  };
}

/** @internal Exported for testing the executable-entrypoint guard. */
export function isMainModule(argvPath) {
  if (typeof argvPath !== "string" || argvPath === "") {
    return false;
  }

  try {
    return import.meta.url === pathToFileURL(argvPath).href;
  } catch {
    return false;
  }
}

// startup-failure does not prove the reviewer never started. It is the class
// for "no classifiable execution output", and several causes share that shape:
// the reviewer never started; usage exhaustion killed it before any usage
// signal was recorded; the step was cancelled; or the execution-file write
// itself failed. claude-code-action writes that file with whatever messages
// accumulated — from its SDK catch handler on a thrown request, and from the
// post-loop path when the stream ends with no result — so a first-request
// refusal lands a literal "[]" or an init-only stream, exactly what a non-usage
// startup error lands. The refusal reason survives only as prose: the review
// step's job log ("SDK execution error: …") and its ::error:: annotation
// ("Action failed with error: …"). Neither is an action output, and neither is
// reachable from a later step in the same job.
//
// Do not infer usage from message *shape* — no typed usage evidence exists on
// this path. The step annotation names the causes instead of asserting one.
//
// Verified against anthropics/claude-code-action base-action/src/run-claude-sdk.ts
// and base-action/src/execution-file.ts as of 2026-07; the root action exposes
// no conclusion output. If upstream adds a typed failure output or changes the
// execution-file envelope, revisit this note and the startup-failure annotation.
export function classifyMessages(messages) {
  if (!Array.isArray(messages) || messages.length === 0) {
    return "startup-failure";
  }

  const assistantErrors = new Set(
    messages
      .filter((message) => isRecord(message) && message.type === "assistant")
      .map((message) => message.error)
      .filter((error) => typeof error === "string")
  );
  const authFailed =
    assistantErrors.has("authentication_failed") ||
    messages.some(
      (message) =>
        isRecord(message) &&
        message.type === "auth_status" &&
        typeof message.error === "string" &&
        message.error.length > 0
    );
  const usageRejected =
    USAGE_ASSISTANT_ERRORS.some((error) => assistantErrors.has(error)) ||
    messages.some(
      (message) =>
        isRecord(message) &&
        message.type === "rate_limit_event" &&
        isRecord(message.rate_limit_info) &&
        message.rate_limit_info.status === "rejected"
    );

  // A broken credential is deterministic across reruns, so it outranks usage.
  if (authFailed) {
    return "auth-failed";
  }
  if (usageRejected) {
    return "out-of-usage";
  }

  const result = [...messages]
    .reverse()
    .find((message) => isRecord(message) && message.type === "result");
  if (!result) {
    return "startup-failure";
  }

  const turns = Number(result.num_turns ?? 0);
  const cost = Number(result.total_cost_usd ?? 0);
  if (turns > 0 || cost > 0) {
    return "reviewer-errored";
  }
  return "unknown";
}

export function classifyExecutionFile({ actionOutcome, fileContent }) {
  if (actionOutcome === "success") {
    return "completed";
  }
  if (typeof fileContent !== "string" || fileContent.trim() === "") {
    return "startup-failure";
  }

  let messages;
  try {
    messages = JSON.parse(fileContent);
  } catch (error) {
    // Output exists but is unparseable (truncated write, format change) — the
    // reviewer DID start, so this is not startup-failure, and there is no
    // rerun-worthy evidence either. Escalate loudly.
    const { message } = getErrorDetails(error);
    process.stderr.write(`execution file unparseable (${fileContent.length} bytes): ${message}\n`);
    return "unknown";
  }
  return classifyMessages(messages);
}

export function main({ argv, env }) {
  const executionFilePath = argv[0] ?? "";
  const actionOutcome = env.ACTION_OUTCOME ?? "";

  let fileContent = null;
  if (executionFilePath !== "") {
    try {
      fileContent = readFileSync(executionFilePath, "utf8");
    } catch (error) {
      // ENOENT means no execution output was written; on a failed outcome that
      // is startup-failure (see the note above classifyMessages). Any other read
      // failure (EACCES, EISDIR, too large) means output may exist, so on a
      // failed outcome escalate to unknown rather than guessing. A successful
      // outcome is already completed regardless of readability.
      const { code, message } = getErrorDetails(error);
      if (code !== "ENOENT" && actionOutcome !== "success") {
        process.stderr.write(`execution file unreadable (${code}): ${message}\n`);
        return "unknown";
      }
      fileContent = null;
    }
  }

  return classifyExecutionFile({ actionOutcome, fileContent });
}

if (isMainModule(process.argv[1])) {
  process.stdout.write(`${main({ argv: process.argv.slice(2), env: process.env })}\n`);
}
