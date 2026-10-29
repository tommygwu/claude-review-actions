#!/usr/bin/env node

import { appendFileSync, readFileSync } from "node:fs";
import process from "node:process";
import { pathToFileURL } from "node:url";

import {
  getErrorDetails,
  main as classifyExecution,
} from "../classify-claude-review/classify-claude-review-execution.mjs";

const REVIEWER_LOGIN = "claude[bot]";
const EXECUTION_HEAD_BYTES = 800;

export const CLASS_ANNOTATIONS = Object.freeze({
  "never-posted":
    "claude-review finished but posted no review comment for head {head} (class=never-posted). A rerun should succeed.",
  "out-of-usage":
    "claude-review posted no review comment for head {head} because the Claude provider rejected the run for usage limits (class=out-of-usage). A rerun hits the same limit until usage resets.",
  "auth-failed":
    "claude-review posted no review comment for head {head} because provider authentication failed (class=auth-failed). Repair the CLAUDE_CODE_OAUTH_TOKEN secret; reruns cannot fix a broken credential.",
  "workflow-modified":
    "claude-review was skipped for head {head} because this PR modifies the review workflow (class=workflow-modified). Reruns cannot fix this; review the workflow change by hand.",
  "reviewer-errored":
    "claude-review ran but errored before posting a review comment for head {head} (class=reviewer-errored). Likely transient: a rerun should succeed.",
  "startup-failure":
    "claude-review produced no classifiable execution output for head {head} (class=startup-failure). Usage exhaustion before any usage signal, a cancelled review step, or a failed execution-file write look the same; read the review step's log before spending a rerun.",
  unknown: "claude-review posted no review comment for head {head} and left no classifiable evidence (class=unknown).",
});

function timestamp(value) {
  if (typeof value !== "string") {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : Math.floor(parsed / 1000);
}

/** Count in-window claude[bot] comments from a `gh api --paginate --slurp` payload. */
export function countReviewComments(payload, { marker, start }) {
  if (!Array.isArray(payload)) {
    throw new TypeError("comments payload must be a list");
  }
  const comments = payload.length > 0 && payload.every(Array.isArray) ? payload.flat() : payload;
  let posted = 0;
  let marked = 0;
  for (const comment of comments) {
    if (typeof comment !== "object" || comment === null || Array.isArray(comment)) {
      throw new TypeError("comment must be an object");
    }
    const createdAt = timestamp(comment.created_at);
    if (
      comment.user?.login === REVIEWER_LOGIN &&
      typeof comment.body === "string" &&
      createdAt !== null &&
      createdAt >= start
    ) {
      posted += 1;
      if (comment.body.includes(marker)) {
        marked += 1;
      }
    }
  }
  return { posted, marked };
}

function executionHead(executionFile) {
  if (!executionFile) {
    return null;
  }
  try {
    return readFileSync(executionFile, "utf8").slice(0, EXECUTION_HEAD_BYTES);
  } catch {
    return null;
  }
}

/**
 * Decide the failure class for one review run: "" when a comment was verified.
 * A verified comment wins over every review outcome. With no comment, a
 * successful review step is never-posted and a failed one keeps the
 * classifier's class.
 */
export function evaluate({ commentsText, env, log }) {
  const head = env.HEAD_SHA ?? "";
  const start = Number.parseInt(env.START_TS ?? "", 10);
  if (!Number.isInteger(start)) {
    log(`::error::start_ts is not an integer epoch (${env.START_TS ?? ""}); cannot verify head ${head}.`);
    return "unknown";
  }
  let counts;
  try {
    counts = countReviewComments(JSON.parse(commentsText), { marker: env.RUN_MARKER ?? "", start });
  } catch (error) {
    log(`::error::invalid review-comment evidence for head ${head}: ${getErrorDetails(error).message}`);
    return "unknown";
  }
  if (counts.posted > 0) {
    if (counts.marked === 0) {
      log(
        `::warning::claude[bot] posted ${counts.posted} review comment(s) this run for head ${head} but none carries this run's marker; passing per the best-effort policy.`
      );
    }
    log(`verified ${counts.posted} claude[bot] review comment(s) this run (${counts.marked} marker-matched) for head ${head}`);
    return "";
  }
  if (env.REVIEW_OUTCOME === "success") {
    return "never-posted";
  }
  const failureClass = classifyExecution({
    argv: [env.EXECUTION_FILE ?? ""],
    env: { ACTION_OUTCOME: env.REVIEW_OUTCOME ?? "", RUNNER_TEMP: env.RUNNER_TEMP },
  });
  const executionText = executionHead(env.EXECUTION_FILE);
  if (executionText !== null) {
    log("--- execution file head (diagnostic) ---");
    log(executionText);
    log("--- end execution file head ---");
  }
  return failureClass;
}

export function annotation(failureClass, head) {
  const template = CLASS_ANNOTATIONS[failureClass] ?? CLASS_ANNOTATIONS.unknown;
  return `::error::${template.replace("{head}", head)}`;
}

export function main({ env, log = (line) => process.stdout.write(`${line}\n`) }) {
  let commentsText;
  try {
    commentsText = readFileSync(env.COMMENTS_FILE ?? "", "utf8");
  } catch (error) {
    log(`::error::review comments are unreadable: ${getErrorDetails(error).message}`);
    return "unknown";
  }
  return evaluate({ commentsText, env, log });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const failureClass = main({ env: process.env });
  appendFileSync(process.env.GITHUB_OUTPUT, `class=${failureClass}\n`);
}
