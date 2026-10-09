import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, it } from "node:test";

import {
  classifyExecutionFile,
  classifyMessages,
  FAILURE_CLASSES,
  main,
  readSkipMarker,
  SKIP_MARKER_FILENAME,
} from "./classify-claude-review-execution.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function initMessage() {
  return { type: "system", subtype: "init", session_id: "s-1" };
}

function resultMessage(overrides = {}) {
  return {
    type: "result",
    subtype: "success",
    is_error: true,
    num_turns: 0,
    total_cost_usd: 0,
    permission_denials_count: 0,
    ...overrides,
  };
}

describe("classifyMessages", () => {
  it("classifies real turns and cost with is_error as reviewer-errored, NOT out-of-usage", () => {
    // The originating incident: the reviewer ran 18 turns and spent $0.83,
    // then errored before posting. A usage refusal never does work, so the
    // work-happened signal must route this to the rerun-eligible class.
    const messages = [
      initMessage(),
      resultMessage({ is_error: true, num_turns: 18, total_cost_usd: 0.828587 }),
    ];
    assert.equal(classifyMessages(messages), "reviewer-errored");
  });

  it("classifies a rejected rate_limit_event as out-of-usage even when turns were spent mid-run", () => {
    // Mid-run exhaustion still means a rerun hits the same wall; the typed
    // usage signal must outrank the work-happened discriminator.
    const messages = [
      initMessage(),
      {
        type: "rate_limit_event",
        rate_limit_info: { status: "rejected", rateLimitType: "five_hour" },
      },
      resultMessage({ num_turns: 7, total_cost_usd: 0.4 }),
    ];
    assert.equal(classifyMessages(messages), "out-of-usage");
  });

  it("ignores allowed rate_limit_events — routine telemetry is not a refusal", () => {
    const messages = [
      initMessage(),
      { type: "rate_limit_event", rate_limit_info: { status: "allowed_warning" } },
      resultMessage({ num_turns: 3, total_cost_usd: 0.1 }),
    ];
    assert.equal(classifyMessages(messages), "reviewer-errored");
  });

  for (const error of ["rate_limit", "billing_error"]) {
    it(`classifies a typed assistant ${error} error as out-of-usage`, () => {
      const messages = [initMessage(), { type: "assistant", error, message: {} }, resultMessage()];
      assert.equal(classifyMessages(messages), "out-of-usage");
    });
  }

  it("classifies a typed authentication_failed assistant error as auth-failed", () => {
    const messages = [
      initMessage(),
      { type: "assistant", error: "authentication_failed", message: {} },
      resultMessage(),
    ];
    assert.equal(classifyMessages(messages), "auth-failed");
  });

  it("classifies an auth_status message carrying an error as auth-failed", () => {
    const messages = [
      { type: "auth_status", isAuthenticating: true, error: "OAuth token expired" },
    ];
    assert.equal(classifyMessages(messages), "auth-failed");
  });

  it("ranks auth-failed above out-of-usage when both signals are present — credentials never heal on rerun", () => {
    const messages = [
      { type: "assistant", error: "authentication_failed", message: {} },
      { type: "rate_limit_event", rate_limit_info: { status: "rejected" } },
    ];
    assert.equal(classifyMessages(messages), "auth-failed");
  });

  it("classifies transient server errors by work done, not by the error label", () => {
    const messages = [
      initMessage(),
      { type: "assistant", error: "server_error", message: {} },
      resultMessage({ num_turns: 5, total_cost_usd: 0.2 }),
    ];
    assert.equal(classifyMessages(messages), "reviewer-errored");
  });

  it("classifies a message stream with no result message and no typed signal as startup-failure", () => {
    assert.equal(classifyMessages([initMessage()]), "startup-failure");
  });

  it("classifies an empty message array as startup-failure", () => {
    assert.equal(classifyMessages([]), "startup-failure");
  });

  it("classifies zero turns, zero cost, and no typed signal as unknown — no evidence to spend a rerun on", () => {
    const messages = [initMessage(), resultMessage({ num_turns: 0, total_cost_usd: 0 })];
    assert.equal(classifyMessages(messages), "unknown");
  });
});

describe("classifyExecutionFile", () => {

  it("returns completed for a successful action outcome without reading the file", () => {
    assert.equal(classifyExecutionFile({ actionOutcome: "success", fileContent: null }), "completed");
  });

  it("returns startup-failure when the execution file is missing on a failed outcome", () => {
    assert.equal(
      classifyExecutionFile({ actionOutcome: "failure", fileContent: null }),
      "startup-failure"
    );
  });

  it("returns startup-failure for an empty execution file", () => {
    assert.equal(
      classifyExecutionFile({ actionOutcome: "failure", fileContent: "  \n" }),
      "startup-failure"
    );
  });

  it("returns unknown for unparseable execution-file content — output exists, so the reviewer DID start", () => {
    // A truncated or corrupt file must not claim "never started"
    // (startup-failure maps to a rerun downstream); with no readable
    // evidence, unknown escalates instead of spending a blind rerun.
    assert.equal(
      classifyExecutionFile({ actionOutcome: "failure", fileContent: "not json {" }),
      "unknown"
    );
  });

  // These shapes carry zero typed usage evidence, so the classifier cannot
  // separate usage exhaustion from a genuine startup failure: the action's only
  // inputs are the execution file and the step outcome, and the SDK's thrown
  // reason survives only as prose no later step in the job can read. Pinned so
  // nobody "fixes" the ambiguity with a shape-based guess.
  it("classifies the SDK-threw-before-any-message shape as startup-failure, not out-of-usage", () => {
    // claude-code-action's SDK catch calls writeExecutionFile(messages) with
    // whatever accumulated, so a first-request refusal lands a literal "[]" —
    // a non-empty file carrying zero usage evidence.
    assert.equal(
      classifyExecutionFile({ actionOutcome: "failure", fileContent: "[]" }),
      "startup-failure"
    );
  });

  it("classifies an init-only stream as startup-failure through the file-parsing entrypoint too", () => {
    // Same shape as the classifyMessages assertion above, entered through JSON
    // parsing, so a change at the file layer cannot silently reclassify it.
    assert.equal(
      classifyExecutionFile({
        actionOutcome: "failure",
        fileContent: JSON.stringify([initMessage()]),
      }),
      "startup-failure"
    );
  });

  it("still returns out-of-usage the moment a usage signal exists, even with no result message", () => {
    // The boundary of the case above: detection was never weakened. Whenever
    // the provider's refusal reaches the message array, usage wins over the
    // missing-result path that would otherwise read as startup-failure.
    assert.equal(
      classifyExecutionFile({
        actionOutcome: "failure",
        fileContent: JSON.stringify([
          initMessage(),
          { type: "rate_limit_event", rate_limit_info: { status: "rejected" } },
        ]),
      }),
      "out-of-usage"
    );
  });

  it("keeps auth-failed above out-of-usage on a resultless stream — credentials never heal on rerun", () => {
    assert.equal(
      classifyExecutionFile({
        actionOutcome: "failure",
        fileContent: JSON.stringify([
          { type: "auth_status", isAuthenticating: true, error: "OAuth token expired" },
          { type: "rate_limit_event", rate_limit_info: { status: "rejected" } },
        ]),
      }),
      "auth-failed"
    );
  });

  it("returns workflow-modified when the skip marker says so and no execution file exists", () => {
    assert.equal(
      classifyExecutionFile({
        actionOutcome: "failure",
        fileContent: null,
        skipMarker: { reason: "workflow-modified" },
      }),
      "workflow-modified"
    );
  });

  it("ignores a skip marker when an execution file exists — a stale marker never overrides real evidence", () => {
    assert.equal(
      classifyExecutionFile({
        actionOutcome: "failure",
        fileContent: JSON.stringify([initMessage(), resultMessage({ num_turns: 4, total_cost_usd: 0.2 })]),
        skipMarker: { reason: "workflow-modified" },
      }),
      "reviewer-errored"
    );
  });

  it("keeps startup-failure for a not-executed marker — an unexplained no-op is not the terminal skip", () => {
    assert.equal(
      classifyExecutionFile({
        actionOutcome: "failure",
        fileContent: null,
        skipMarker: { reason: "not-executed" },
      }),
      "startup-failure"
    );
  });

});

describe("main", () => {
  let dir;

  beforeEach(() => {
    dir = mkdtempSync(path.join(tmpdir(), "classify-claude-review-"));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  for (const failureClass of ["completed", "out-of-usage", "auth-failed", "workflow-modified", "reviewer-errored", "never-posted", "startup-failure", "unknown"]) {
    it(`reads a version 1 ${failureClass} envelope through the execution-file boundary`, () => {
      const file = path.join(dir, "text-review-result.json");
      writeFileSync(file, JSON.stringify({ version: 1, repository: "example/repo", pr_number: 7, workflow_run: 81, attempt: 2, failure_class: failureClass, reviewed_head_sha: "a".repeat(40), producer_sha: "b".repeat(40), ...(failureClass === "completed" ? { comment_id: 123 } : {}) }));
      assert.equal(main({ argv: [file], env: { ACTION_OUTCOME: "success" } }), failureClass);
      if (failureClass !== "completed") assert.equal(main({ argv: [file], env: { ACTION_OUTCOME: "failure" } }), failureClass);
    });
  }

  for (const outcome of ["failure", "cancelled"]) {
    it(`does not accept a completed envelope from a ${outcome} action`, () => {
      const file = path.join(dir, "text-review-result.json");
      writeFileSync(file, JSON.stringify({ version: 1, repository: "example/repo", pr_number: 7, workflow_run: 81, attempt: 2, failure_class: "completed", reviewed_head_sha: "a".repeat(40), producer_sha: "b".repeat(40), comment_id: 123 }));
      assert.equal(main({ argv: [file], env: { ACTION_OUTCOME: outcome } }), "reviewer-errored");
    });
  }

  for (const [label, envelope] of [
    ["unsupported version", { version: 2, failure_class: "completed" }],
    ["unknown failure class", { version: 1, failure_class: "retry-anyway" }],
  ]) {
    it(`does not treat ${label} as success because the action step was green`, () => {
      const file = path.join(dir, "text-review-result.json");
      writeFileSync(file, JSON.stringify(envelope));
      assert.equal(main({ argv: [file], env: { ACTION_OUTCOME: "success" } }), "unknown");
      assert.equal(main({ argv: [file], env: { ACTION_OUTCOME: "failure" } }), "unknown");
    });
  }

  it("reads the execution file from argv and classifies it", () => {
    const file = path.join(dir, "claude-execution-output.json");
    writeFileSync(
      file,
      JSON.stringify([
        initMessage(),
        resultMessage({ is_error: true, num_turns: 18, total_cost_usd: 0.828587 }),
      ])
    );
    assert.equal(main({ argv: [file], env: { ACTION_OUTCOME: "failure" } }), "reviewer-errored");
  });

  it("treats a missing file (ENOENT) as startup-failure", () => {
    assert.equal(
      main({ argv: [path.join(dir, "missing.json")], env: { ACTION_OUTCOME: "failure" } }),
      "startup-failure"
    );
  });

  it("treats a non-ENOENT read failure as unknown — output may exist, so never claim the reviewer never started", () => {
    // A directory path raises EISDIR on read.
    assert.equal(main({ argv: [dir], env: { ACTION_OUTCOME: "failure" } }), "unknown");
  });

  it("never escalates an unreadable file on a successful outcome — a green review stays completed", () => {
    // The escalation guard is `code !== "ENOENT" && actionOutcome !== "success"`.
    // Only its failure side was covered, so dropping the outcome half would
    // silently flip a passing review from completed to unknown.
    assert.equal(main({ argv: [dir], env: { ACTION_OUTCOME: "success" } }), "completed");
  });

  it("treats an empty execution_file output as startup-failure on a failed outcome", () => {
    assert.equal(main({ argv: [""], env: { ACTION_OUTCOME: "failure" } }), "startup-failure");
  });

  it("reads the skip marker from RUNNER_TEMP when the review produced no execution file", () => {
    writeFileSync(path.join(dir, SKIP_MARKER_FILENAME), JSON.stringify({ reason: "workflow-modified" }));
    assert.equal(
      main({ argv: [""], env: { ACTION_OUTCOME: "failure", RUNNER_TEMP: dir } }),
      "workflow-modified"
    );
  });

  it("falls back to startup-failure on an unparseable skip marker", () => {
    writeFileSync(path.join(dir, SKIP_MARKER_FILENAME), "not json {");
    assert.equal(readSkipMarker(dir), null);
    assert.equal(
      main({ argv: [""], env: { ACTION_OUTCOME: "failure", RUNNER_TEMP: dir } }),
      "startup-failure"
    );
  });

});

describe("failure-classes.json", () => {
  it("stays in lockstep with the classifier's FAILURE_CLASSES vocabulary", () => {
    // The checked-in artifact is the single source of truth consumers read;
    // it must match the classifier exactly (order included) so a class added
    // to one is never missing from the other.
    const artifact = JSON.parse(readFileSync(path.join(HERE, "failure-classes.json"), "utf8"));
    assert.deepEqual(artifact.classes, [...FAILURE_CLASSES]);
  });

});
