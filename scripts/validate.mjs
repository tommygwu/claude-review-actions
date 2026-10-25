import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const SUITES = ["classify-claude-review/*.test.mjs", "review/*.test.mjs"];

const scope = process.argv[2];
if (process.argv.length !== 3 || (scope !== "changed" && scope !== "full")) {
  console.error("usage: node scripts/validate.mjs changed|full");
  process.exit(2);
}

const completed = spawnSync(process.execPath, ["--test", ...SUITES], {
  cwd: fileURLToPath(new URL("..", import.meta.url)),
  stdio: ["ignore", 2, 2],
});
const python = spawnSync("python3", ["-B", "-m", "unittest", "discover", "-s", ".github/actions/ci-run-context", "-p", "test_*.py"], {
  cwd: fileURLToPath(new URL("..", import.meta.url)),
  stdio: ["ignore", 2, 2],
  env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
});
const schema = spawnSync("python3", ["-B", "scripts/test_run_context_schema.py"], {
  cwd: fileURLToPath(new URL("..", import.meta.url)),
  stdio: ["ignore", 2, 2],
  env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
});
const checks = ["node-test", "ci-run-context", "ci-run-context-schema"];
if (schema.error || schema.status === null) {
  console.log(JSON.stringify({ status: "stopped", scope, checks, reason: "setup-failed",
    detail: { failure: schema.error?.message ?? "validation-process-interrupted", signal: schema.signal } }));
  process.exit(1);
}
if (python.error || python.status === null) {
  console.log(JSON.stringify({ status: "stopped", scope, checks, reason: "setup-failed",
    detail: { failure: python.error?.message ?? "validation-process-interrupted", signal: python.signal } }));
  process.exit(1);
}
if (completed.error || completed.status === null) {
  console.log(JSON.stringify({
    status: "stopped",
    scope,
    checks,
    reason: "setup-failed",
    detail: { failure: completed.error?.message ?? "validation-process-interrupted", signal: completed.signal },
  }));
  process.exit(1);
}
console.log(JSON.stringify(completed.status === 0 && python.status === 0 && schema.status === 0
  ? { status: "pass", scope, checks }
  : { status: "failed", scope, checks, reason: "validation-failed", detail: { failed_check: completed.status !== 0 ? "node-test" : python.status !== 0 ? "ci-run-context" : "ci-run-context-schema" } }));
process.exit(completed.status || python.status || schema.status);
