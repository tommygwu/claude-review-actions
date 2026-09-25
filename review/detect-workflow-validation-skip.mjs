#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { appendFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

// claude-code-action exits success without running when its token exchange
// reports "Workflow validation failed": the running workflow file differs from
// the default branch's copy. It sets `skipped_due_to_workflow_validation_mismatch`
// on its inner step, but the root action does not expose that output, so this
// script infers the skip from what is exposed (see review/README.md).
//
// Verified against anthropics/claude-code-action src/entrypoints/run.ts and
// src/github/token.ts at v1 (2026-09): with a non-empty prompt, the only path
// that returns success without setting `conclusion` is the validation skip.

export const SKIP_MARKER_FILENAME = "claude-review-skip.json";

export const SKIP_REASONS = Object.freeze(["workflow-modified", "not-executed"]);

export function workflowPathFromRef(workflowRef, repository) {
  if (typeof workflowRef !== "string" || typeof repository !== "string" || repository === "") {
    return null;
  }
  const at = workflowRef.lastIndexOf("@");
  const qualified = at === -1 ? workflowRef : workflowRef.slice(0, at);
  const prefix = `${repository}/`;
  if (!qualified.toLowerCase().startsWith(prefix.toLowerCase())) {
    return null;
  }
  const workflowPath = qualified.slice(prefix.length);
  return workflowPath === "" ? null : workflowPath;
}

// comparison.status: "differs" | "identical" | "unverified".
// An unverified diff still means workflow-modified: the action already returned
// success without running, which it only does on the validation skip.
export function decideSkipReason({ promptPresent, comparison }) {
  if (!promptPresent) {
    return "not-executed";
  }
  return comparison.status === "identical" ? "not-executed" : "workflow-modified";
}

function localBlob({ ref, workflowPath, cwd }) {
  try {
    const sha = execFileSync("git", ["rev-parse", "--verify", "--quiet", `${ref}:${workflowPath}`], {
      cwd,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    return sha === "" ? null : sha;
  } catch {
    return null;
  }
}

// Returns the blob SHA, or "" when the file does not exist at that ref.
async function apiBlob({ apiUrl, repository, ref, workflowPath, token, fetchImpl }) {
  const encodedPath = workflowPath.split("/").map(encodeURIComponent).join("/");
  const url = `${apiUrl}/repos/${repository}/contents/${encodedPath}?ref=${encodeURIComponent(ref)}`;
  const response = await fetchImpl(url, {
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
  if (response.status === 404) {
    return "";
  }
  if (!response.ok) {
    throw new Error(`contents API returned ${response.status} for ref ${ref}`);
  }
  const body = await response.json();
  if (typeof body?.sha !== "string") {
    throw new Error(`contents API returned no blob sha for ref ${ref}`);
  }
  return body.sha;
}

// Mirrors the upstream check: the running workflow file (at workflow_sha) must
// match the default branch. The default branch always resolves through the API
// because a local remote-tracking ref can be stale.
export async function compareWorkflowFile({
  workflowPath,
  workflowSha,
  defaultBranch,
  repository,
  apiUrl,
  token,
  cwd,
  fetchImpl = globalThis.fetch,
  readLocalBlob = localBlob,
}) {
  if (!workflowPath || !workflowSha || !defaultBranch) {
    return { status: "unverified", detail: "workflow path, workflow SHA, or default branch is unknown" };
  }
  try {
    const api = { apiUrl, repository, workflowPath, token, fetchImpl };
    const running =
      readLocalBlob({ ref: workflowSha, workflowPath, cwd }) ??
      (await apiBlob({ ...api, ref: workflowSha }));
    const onDefault = await apiBlob({ ...api, ref: defaultBranch });
    return { status: running === onDefault ? "identical" : "differs", detail: "" };
  } catch (error) {
    return { status: "unverified", detail: error instanceof Error ? error.message : String(error) };
  }
}

export function annotationFor({ reason, workflowPath, comparison }) {
  const file = workflowPath ?? "the review workflow";
  if (reason === "workflow-modified") {
    const unverified =
      comparison.status === "unverified"
        ? ` The diff against the default branch could not be confirmed (${comparison.detail}), but claude-code-action returned success without running, which it only does on this skip.`
        : "";
    return (
      `::error::claude-code-action skipped the review by design because this PR modifies ${file}, ` +
      `which no longer matches the default branch (skip_reason=workflow-modified). ` +
      `Reruns cannot fix this. The review runs on the next PR after this one merges; ` +
      `a maintainer must review the workflow change by hand.${unverified}`
    );
  }
  const cause =
    comparison.status === "identical"
      ? `${file} matches the default branch, so this is not the workflow-validation skip`
      : "the prompt input was empty";
  return (
    `::error::claude-code-action returned success without running the reviewer (skip_reason=not-executed): ` +
    `${cause}. Read the review step log.`
  );
}

export async function main({ env, fetchImpl = globalThis.fetch, readLocalBlob = localBlob }) {
  const workflowPath = workflowPathFromRef(env.WORKFLOW_REF, env.REPOSITORY);
  const comparison = await compareWorkflowFile({
    workflowPath,
    workflowSha: env.WORKFLOW_SHA,
    defaultBranch: env.DEFAULT_BRANCH,
    repository: env.REPOSITORY,
    apiUrl: env.GITHUB_API_URL || "https://api.github.com",
    token: env.GITHUB_TOKEN_FOR_CHECK,
    cwd: env.GITHUB_WORKSPACE || process.cwd(),
    fetchImpl,
    readLocalBlob,
  });
  const reason = decideSkipReason({ promptPresent: env.PROMPT_PRESENT === "true", comparison });

  if (env.RUNNER_TEMP) {
    writeFileSync(
      path.join(env.RUNNER_TEMP, SKIP_MARKER_FILENAME),
      `${JSON.stringify({ version: 1, reason, workflow_path: workflowPath, comparison: comparison.status })}\n`
    );
  }
  if (env.GITHUB_OUTPUT) {
    appendFileSync(env.GITHUB_OUTPUT, `skip_reason=${reason}\n`);
  }
  return { reason, annotation: annotationFor({ reason, workflowPath, comparison }) };
}

if (typeof process.argv[1] === "string" && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { annotation } = await main({ env: process.env });
  process.stdout.write(`${annotation}\n`);
  process.exitCode = 1;
}
