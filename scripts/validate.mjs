import { spawnSync } from "node:child_process";
import { existsSync, lstatSync, realpathSync } from "node:fs";
import { devNull } from "node:os";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

const SUITES = ["classify-claude-review/*.test.mjs", "review/*.test.mjs"];
const root = fileURLToPath(new URL("..", import.meta.url));
const environmentPath = join(root, ".venv");
const pythonPath = join(environmentPath, process.platform === "win32" ? "Scripts/python.exe" : "bin/python3");

const scope = process.argv[2];
if (process.argv.length !== 3 || !["setup", "changed", "full"].includes(scope)) {
  console.error("usage: node scripts/validate.mjs setup|changed|full");
  process.exit(2);
}

if (scope === "setup") {
  const selectedPython = process.env.UV_PYTHON;
  const fail = (message) => {
    console.error(message);
    process.exit(1);
  };
  if (!selectedPython || !isAbsolute(selectedPython)) {
    fail("Setup requires the declared Python interpreter in UV_PYTHON.");
  }
  if (lstatSync(environmentPath, { throwIfNoEntry: false })?.isSymbolicLink()) {
    fail("Setup requires a local .venv directory, not a symbolic link.");
  }
  if (!existsSync(pythonPath)) {
    const created = spawnSync(selectedPython, ["-m", "venv", environmentPath], {
      cwd: root, stdio: ["ignore", 2, 2], timeout: 60000,
    });
    if (created.error || created.status !== 0) fail("Could not create the declared Python environment.");
  }
  const selected = spawnSync(selectedPython, ["-c", "import sys; print(sys.prefix)"], { encoding: "utf8", timeout: 10000 });
  const environment = spawnSync(pythonPath, ["-c", "import sys; print(sys.prefix); print(sys.base_prefix)"], { encoding: "utf8", timeout: 10000 });
  const prefixes = environment.stdout?.trim().split("\n") ?? [];
  if (selected.error || environment.error || selected.status !== 0 || environment.status !== 0
      || prefixes.length !== 2
      || realpathSync(prefixes[0]) !== realpathSync(environmentPath)
      || realpathSync(prefixes[0]) === realpathSync(prefixes[1])
      || realpathSync(selected.stdout.trim()) !== realpathSync(prefixes[1])) {
    fail("The local .venv does not match the declared interpreter; use a clean worktree.");
  }
  const installed = spawnSync(pythonPath, ["-m", "pip", "--isolated", "--require-virtualenv", "install", "--disable-pip-version-check", "--no-input", "--timeout", "15", "--retries", "0", "--require-hashes", "-r", "requirements-schema-tests.txt"], {
    cwd: root, stdio: ["ignore", 2, 2], timeout: 60000,
    env: { ...process.env, PIP_CONFIG_FILE: devNull },
  });
  if (installed.error || installed.status !== 0) fail("Immutable schema dependency setup failed.");
  console.log(JSON.stringify({ status: "pass", scope, checks: ["schema-dependencies"] }));
  process.exit(0);
}

const completed = spawnSync(process.execPath, ["--test", ...SUITES], {
  cwd: fileURLToPath(new URL("..", import.meta.url)),
  stdio: ["ignore", 2, 2],
});
const python = spawnSync(pythonPath, ["-B", "-m", "unittest", "discover", "-s", ".github/actions/ci-run-context", "-p", "test_*.py"], {
  cwd: fileURLToPath(new URL("..", import.meta.url)),
  stdio: ["ignore", 2, 2],
  env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
});
const schema = spawnSync(pythonPath, ["-B", "scripts/test_run_context_schema.py"], {
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
