import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
const unit = process.argv[2];
const directories = unit === "classifier" ? ["classify-claude-review"] : unit === "review" ? ["review"] : [];
const files = directories.flatMap(dir => readdirSync(dir).filter(name => name.endsWith(".test.mjs")).map(name => `${dir}/${name}`));
if (!files.length) throw new Error("No tests selected");
const result = spawnSync(process.execPath, ["--test", ...files], { stdio: "inherit" });
if (result.error) throw result.error;
process.exit(result.status ?? 1);
