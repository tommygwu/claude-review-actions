import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, it } from "node:test";

import { main } from "./verify-claude-review.mjs";

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

function env({ comments, reviewOutcome, execution }) {
  const commentsFile = path.join(dir, "comments.json");
  writeFileSync(commentsFile, typeof comments === "string" ? comments : JSON.stringify(comments));
  let executionFile = "";
  if (execution !== undefined) {
    executionFile = path.join(dir, "execution.json");
    writeFileSync(executionFile, JSON.stringify(execution));
  }
  return {
    COMMENTS_FILE: commentsFile,
    RUN_MARKER: MARKER,
    START_TS: String(START),
    HEAD_SHA: "abc",
    REVIEW_OUTCOME: reviewOutcome,
    EXECUTION_FILE: executionFile,
    RUNNER_TEMP: dir,
  };
}

function classOf(inputs) {
  return main({ env: env(inputs), log: () => {} });
}

function runBlock(stepId) {
  const lines = readFileSync(path.join(HERE, "action.yml"), "utf8").split("\n");
  const start = lines.findIndex((line) => line.trim() === `- id: ${stepId}`);
  const run = lines.findIndex((line, index) => index > start && line.trim() === "run: |");
  const indent = lines[run].search(/\S/);
  const body = [];
  for (const line of lines.slice(run + 1)) {
    if (line.trim() !== "" && line.search(/\S/) <= indent) {
      break;
    }
    body.push(line.slice(indent + 2));
  }
  return body.join("\n");
}

const USAGE_REJECTED = [
  { type: "system", subtype: "init" },
  { type: "rate_limit_event", rate_limit_info: { status: "rejected" } },
];

describe("verify-claude-review", () => {
  it("labels a successful review that posted nothing never-posted", () => {
    // openclaw-cake#413 run 40722142710: the review step succeeded, posted
    // nothing, and the old guard skipped the classifier, leaving `unknown`.
    // Runs the action's evaluate step with the default expect_comment.
    const bin = path.join(dir, "bin");
    mkdirSync(bin);
    writeFileSync(path.join(bin, "gh"), "#!/usr/bin/env bash\necho '[[]]'\n", { mode: 0o755 });
    const outputFile = path.join(dir, "github-output");
    writeFileSync(outputFile, "");
    const result = spawnSync("bash", ["-c", runBlock("evaluate")], {
      env: {
        ...process.env,
        ...env({ comments: [], reviewOutcome: "success" }),
        PATH: `${bin}${path.delimiter}${process.env.PATH}`,
        GITHUB_OUTPUT: outputFile,
        ACTION_PATH: HERE,
        EXPECT_COMMENT: "true",
        REPO: "acme/app",
        PR_NUMBER: "1",
      },
      encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(readFileSync(outputFile, "utf8"), "class=never-posted\n");
  });

  it("passes a posted comment even when the review step then failed", () => {
    assert.equal(classOf({ comments: [[comment()]], reviewOutcome: "failure", execution: USAGE_REJECTED }), "");
  });

  it("keeps the classifier class for a failed review that posted nothing", () => {
    assert.equal(classOf({ comments: [[]], reviewOutcome: "failure", execution: USAGE_REJECTED }), "out-of-usage");
  });

  it("reads the skip marker so a workflow self-edit stays workflow-modified", () => {
    writeFileSync(path.join(dir, "claude-review-skip.json"), JSON.stringify({ reason: "workflow-modified" }));
    assert.equal(classOf({ comments: [[]], reviewOutcome: "failure" }), "workflow-modified");
  });

  it("passes a claude[bot] comment that lost the run marker", () => {
    assert.equal(classOf({ comments: [[comment({ body: "# Code Review" })]], reviewOutcome: "success" }), "");
  });

  it("counts only claude[bot] comments inside the run window, on any page", () => {
    const forged = [[comment({ login: "octocat" })], [comment({ at: START - 1 })]];
    assert.equal(classOf({ comments: forged, reviewOutcome: "success" }), "never-posted");
    assert.equal(classOf({ comments: [...forged, [comment()]], reviewOutcome: "failure" }), "");
  });

  it("fails closed on an unparseable comments payload", () => {
    assert.equal(classOf({ comments: "not json", reviewOutcome: "success" }), "unknown");
  });
});

describe("report step", () => {
  it("fails the step and prints the failure marker last", () => {
    const result = spawnSync("bash", ["-c", runBlock("report")], {
      env: { ...process.env, FAILURE_CLASS: "never-posted", HEAD_SHA: "abc", ACTION_PATH: HERE },
      encoding: "utf8",
    });
    assert.equal(result.status, 1, result.stderr);
    assert.equal(result.stdout.trimEnd().split("\n").at(-1), "CLAUDE_REVIEW_FAILURE_CLASS=never-posted");
  });
});
