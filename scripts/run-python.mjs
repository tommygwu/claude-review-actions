import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const python = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python3");
const result = spawnSync(python, process.argv.slice(2), { cwd: root, stdio: "inherit" });
if (result.error || result.signal) {
  console.error(result.error?.message ?? `Python interrupted: ${result.signal}`);
}
process.exit(result.status ?? 1);
