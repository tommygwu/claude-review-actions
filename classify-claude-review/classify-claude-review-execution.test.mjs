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
  getErrorDetails,
  isMainModule,
  main,
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
  it("keeps diagnostics safe when a parser throws a primitive", () => {
    assert.deepEqual(getErrorDetails("unexpected parser failure"), {
      code: "unknown",
      message: "unexpected parser failure",
    });
    assert.deepEqual(getErrorDetails(null), { code: "unknown", message: "null" });
  });

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

  it("only ever returns classes from the closed vocabulary github-fix keys on", () => {
    const samples = [
      classifyExecutionFile({ actionOutcome: "success", fileContent: null }),
      classifyExecutionFile({ actionOutcome: "failure", fileContent: null }),
      classifyExecutionFile({
        actionOutcome: "failure",
        fileContent: JSON.stringify([resultMessage({ num_turns: 2 })]),
      }),
    ];
    for (const sample of samples) {
      assert.ok(FAILURE_CLASSES.includes(sample));
    }
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

  it("treats an empty execution_file output as startup-failure on a failed outcome", () => {
    assert.equal(main({ argv: [""], env: { ACTION_OUTCOME: "failure" } }), "startup-failure");
  });

  it("does not evaluate the executable entrypoint when argv is absent", () => {
    assert.equal(isMainModule(undefined), false);
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
