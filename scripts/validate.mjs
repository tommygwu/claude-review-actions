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
const checks = ["node-test"];
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
console.log(JSON.stringify(completed.status === 0
  ? { status: "pass", scope, checks }
  : { status: "failed", scope, checks, reason: "validation-failed", detail: { failed_check: "node-test" } }));
process.exit(completed.status);
