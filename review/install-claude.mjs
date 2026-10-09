import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, writeFileSync, chmodSync, appendFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";

export async function installClaude(outputRoot = tmpdir()) {
  const lock = JSON.parse(readFileSync(new URL("./claude-code-lock.json", import.meta.url), "utf8"));
  const pin = lock.platforms[`${process.platform}-${process.arch}`];
  if (!pin) throw new Error("Unsupported Claude CLI platform");
  const root = mkdtempSync(path.join(outputRoot, "claude-cli-"));
  const response = await fetch(pin.url, { redirect: "error", signal: AbortSignal.timeout(120_000) });
  if (!response.ok) throw new Error("Pinned Claude CLI download failed");
  const archive = Buffer.from(await response.arrayBuffer());
  if (`sha512-${createHash("sha512").update(archive).digest("base64")}` !== pin.integrity) throw new Error("Claude CLI archive integrity mismatch");
  const archivePath = path.join(root, "cli.tgz");
  writeFileSync(archivePath, archive, { mode: 0o600 });
  const executable = path.join(root, "claude");
  const binary = execFileSync("/usr/bin/tar", ["-xOzf", archivePath, "package/claude"], { maxBuffer: 300_000_000, env: { PATH: "/usr/bin:/bin" } });
  writeFileSync(executable, binary, { mode: 0o700 }); chmodSync(executable, 0o700);
  const version = execFileSync(executable, ["--version"], { env: { HOME: root, PATH: "/usr/bin:/bin", DISABLE_AUTOUPDATER: "1" }, encoding: "utf8", timeout: 15_000 }).trim();
  if (version !== `${lock.version} (Claude Code)`) throw new Error("Claude CLI version mismatch");
  return executable;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const executable = await installClaude(process.env.RUNNER_TEMP || tmpdir());
  if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `executable=${executable}\n`);
  else console.log(executable);
}
