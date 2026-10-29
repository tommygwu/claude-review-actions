import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, it } from "node:test";

import { FAILURE_CLASSES } from "../classify-claude-review/classify-claude-review-execution.mjs";
import { annotation, CLASS_ANNOTATIONS, countReviewComments, main } from "./verify-claude-review.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const START = 1_800_000_000;
const MARKER = "<!-- claude-review-run:1:1:abc -->";

function comment({ login = "claude[bot]", body = `# Code Review\n${MARKER}`, at = START + 5 } = {}) {
  return { user: { login }, body, created_at: new Date(at * 1000).toISOString() };
}

let dir;
beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), "verify-claude-review-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function run({ comments, reviewOutcome, execution }) {
  const commentsFile = path.join(dir, "comments.json");
  writeFileSync(commentsFile, typeof comments === "string" ? comments : JSON.stringify(comments));
  let executionFile = "";
  if (execution !== undefined) {
    executionFile = path.join(dir, "execution.json");
    writeFileSync(executionFile, JSON.stringify(execution));
  }
  const lines = [];
  const failureClass = main({
    env: {
      COMMENTS_FILE: commentsFile,
      RUN_MARKER: MARKER,
      START_TS: String(START),
      HEAD_SHA: "abc",
      REVIEW_OUTCOME: reviewOutcome,
      EXECUTION_FILE: executionFile,
      RUNNER_TEMP: dir,
    },
    log: (line) => lines.push(line),
  });
  return { failureClass, lines };
}

const USAGE_REJECTED = [
  { type: "system", subtype: "init" },
  { type: "rate_limit_event", rate_limit_info: { status: "rejected" } },
];

describe("verify-claude-review", () => {
  it("labels a successful review that posted nothing never-posted", () => {
    // openclaw-cake#413 run 40722142710: the review step succeeded, posted
    // nothing, and the old guard skipped the classifier, leaving `unknown`.
    assert.equal(run({ comments: [[]], reviewOutcome: "success" }).failureClass, "never-posted");
  });

  it("passes a posted comment even when the review step then failed", () => {
    const result = run({ comments: [[comment()]], reviewOutcome: "failure", execution: USAGE_REJECTED });
    assert.equal(result.failureClass, "");
  });

  it("keeps the classifier class for a failed review that posted nothing", () => {
    const result = run({ comments: [[]], reviewOutcome: "failure", execution: USAGE_REJECTED });
    assert.equal(result.failureClass, "out-of-usage");
  });

  it("reads the skip marker so a workflow self-edit stays workflow-modified", () => {
    writeFileSync(path.join(dir, "claude-review-skip.json"), JSON.stringify({ reason: "workflow-modified" }));
    assert.equal(run({ comments: [[]], reviewOutcome: "failure" }).failureClass, "workflow-modified");
  });

  it("passes a markerless claude[bot] comment with a warning", () => {
    const result = run({ comments: [[comment({ body: "# Code Review" })]], reviewOutcome: "success" });
    assert.equal(result.failureClass, "");
    assert.ok(result.lines.some((line) => line.startsWith("::warning::")));
  });

  it("fails closed on an unparseable comments payload", () => {
    assert.equal(run({ comments: "not json", reviewOutcome: "success" }).failureClass, "unknown");
  });
});

describe("countReviewComments", () => {
  it("counts only in-window claude[bot] comments across slurped pages", () => {
    const payload = [
      [comment(), comment({ login: "octocat" })],
      [comment({ at: START - 1 }), comment({ body: "late, no marker" })],
    ];
    assert.deepEqual(countReviewComments(payload, { marker: MARKER, start: START }), { posted: 2, marked: 1 });
  });
});

describe("action contract", () => {
  it("annotates every class the action can emit", () => {
    for (const failureClass of FAILURE_CLASSES.filter((value) => value !== "completed")) {
      assert.ok(CLASS_ANNOTATIONS[failureClass], `missing annotation for ${failureClass}`);
    }
    assert.match(annotation("never-posted", "abc"), /^::error::.*head abc .*class=never-posted/);
  });

  it("prints the failure marker as the report step's last output", () => {
    const actionYml = readFileSync(path.join(HERE, "action.yml"), "utf8");
    const report = actionYml.slice(actionYml.indexOf("- id: report"));
    const outputs = report.split("\n").filter((line) => /^\s+(echo|node|console\.log)/.test(line));
    assert.match(outputs.at(-1), /echo "CLAUDE_REVIEW_FAILURE_CLASS=\$\{FAILURE_CLASS\}"$/);
  });
});
