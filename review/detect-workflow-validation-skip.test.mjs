import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import {
  annotationFor,
  compareWorkflowFile,
  decideSkipReason,
  workflowPathFromRef,
} from "./detect-workflow-validation-skip.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function apiReturning(byRef) {
  const calls = [];
  const fetchImpl = async (url) => {
    const ref = new URL(url).searchParams.get("ref");
    calls.push(ref);
    const entry = byRef[ref];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    if (typeof entry === "number") {
      return { ok: false, status: entry, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => ({ sha: entry }) };
  };
  return { fetchImpl, calls };
}

const base = {
  workflowPath: ".github/workflows/claude-code-review.yml",
  workflowSha: "merge-sha",
  defaultBranch: "main",
  repository: "o/r",
  apiUrl: "https://api.github.com",
  token: "t",
  cwd: HERE,
};

describe("workflowPathFromRef", () => {
  it("strips the repository prefix and the ref suffix", () => {
    assert.equal(
      workflowPathFromRef("o/r/.github/workflows/claude-code-review.yml@refs/pull/77/merge", "o/r"),
      ".github/workflows/claude-code-review.yml"
    );
  });

  it("matches the repository case-insensitively", () => {
    assert.equal(workflowPathFromRef("O/R/.github/workflows/a.yml@refs/heads/x", "o/r"), ".github/workflows/a.yml");
  });

  it("returns null for a ref from another repository — never compare the wrong file", () => {
    assert.equal(workflowPathFromRef("other/r/.github/workflows/a.yml@refs/heads/x", "o/r"), null);
  });
});

describe("compareWorkflowFile", () => {
  it("reports differs when the running file's blob is not the default branch's", async () => {
    const { fetchImpl, calls } = apiReturning({ main: "blob-main" });
    const result = await compareWorkflowFile({ ...base, fetchImpl, readLocalBlob: () => "blob-pr" });
    assert.equal(result.status, "differs");
    assert.deepEqual(calls, ["main"], "a local blob for the immutable workflow SHA must skip its API call");
  });

  it("falls back to the API for the running file when the checkout lacks it", async () => {
    const { fetchImpl, calls } = apiReturning({ "merge-sha": "same", main: "same" });
    const result = await compareWorkflowFile({ ...base, fetchImpl, readLocalBlob: () => null });
    assert.equal(result.status, "identical");
    assert.deepEqual(calls, ["merge-sha", "main"]);
  });

  it("reads the running file's blob from the local checkout with real git", async () => {
    const localSha = execFileSync("git", ["rev-parse", "HEAD:review/action.yml"], { cwd: HERE, encoding: "utf8" }).trim();
    const { fetchImpl, calls } = apiReturning({ main: localSha });
    const result = await compareWorkflowFile({ ...base, workflowPath: "review/action.yml", workflowSha: "HEAD", fetchImpl });
    assert.equal(result.status, "identical");
    assert.deepEqual(calls, ["main"]);
  });

  it("reports differs when the default branch has no such file — the PR adds the workflow", async () => {
    const { fetchImpl } = apiReturning({});
    const result = await compareWorkflowFile({ ...base, fetchImpl, readLocalBlob: () => "blob-pr" });
    assert.equal(result.status, "differs");
  });

  it("reports unverified, not identical, when the API fails", async () => {
    const { fetchImpl } = apiReturning({ main: 403 });
    const result = await compareWorkflowFile({ ...base, fetchImpl, readLocalBlob: () => "blob-pr" });
    assert.equal(result.status, "unverified");
    assert.match(result.detail, /403/);
  });
});

describe("decideSkipReason", () => {
  it("names workflow-modified for a differing or unverifiable file", () => {
    for (const status of ["differs", "unverified"]) {
      assert.equal(decideSkipReason({ promptPresent: true, comparison: { status } }), "workflow-modified");
    }
  });

  it("names not-executed when the file matches, so an unexplained no-op is not called terminal", () => {
    assert.equal(
      decideSkipReason({ promptPresent: true, comparison: { status: "identical" } }),
      "not-executed"
    );
  });

  it("names not-executed for an empty prompt — upstream skips with no trigger, not validation", () => {
    assert.equal(
      decideSkipReason({ promptPresent: false, comparison: { status: "differs" } }),
      "not-executed"
    );
  });
});

describe("annotationFor", () => {
  it("tells the operator the skip is by design and a rerun cannot fix it", () => {
    const text = annotationFor({
      reason: "workflow-modified",
      workflowPath: ".github/workflows/claude-code-review.yml",
      comparison: { status: "differs", detail: "" },
    });
    assert.match(text, /^::error::/);
    assert.match(text, /Reruns cannot fix/);
    assert.match(text, /next PR after this one merges/);
    assert.match(text, /by hand/);
  });
});

describe("review/action.yml wiring", () => {
  it("gates the skip check on a missing conclusion so a normal run never reaches it", () => {
    const actionYml = readFileSync(path.join(HERE, "action.yml"), "utf8");
    assert.match(
      actionYml,
      /if: success\(\) && steps\.review\.outputs\.conclusion == '' && steps\.review\.outputs\.execution_file == ''/
    );
    assert.match(actionYml, /value: \$\{\{ steps\.skip-check\.outputs\.skip_reason \}\}/);
  });
});
