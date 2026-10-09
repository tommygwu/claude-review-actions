import { readFileSync } from "node:fs";
import { execFileSync, spawnSync } from "node:child_process";

const declaration = JSON.parse(readFileSync(".repository.json", "utf8"));
const plan = JSON.parse(readFileSync(".repository-suite-plan.json", "utf8"));
const version = readFileSync(declaration.runtime.version_source, "utf8").trim();
if (process.version !== `v${version}`) throw new Error(`Node ${version} is required`);
const scope = process.argv[2];
if (!["changed", "full"].includes(scope)) throw new Error("Use changed or full scope");
const git = args => execFileSync("git", args, { encoding: "utf8" }).trim().split("\n").filter(Boolean);
if (git(["status", "--porcelain"]).length) throw new Error("Checks require a clean committed candidate");
const base = scope === "changed" ? git(["merge-base", "HEAD", process.env.BASE_REV || "origin/main"])[0] : null;
const paths = scope === "changed" ? git(["diff", "--no-renames", "--name-only", base, "HEAD"]) : git(["ls-files"]);
const patterns = [...declaration.gates.shared, ...declaration.gates.inert, ...Object.values(declaration.gates.units).flatMap(x => x.inputs)];
const matching = pats => pats.length ? [...git(["ls-files", "--", ...pats.map(x => `:(glob)${x}`)]), ...(base ? git(["diff", "--no-renames", "--name-only", base, "HEAD", "--", ...pats.map(x => `:(glob)${x}`)]) : [])] : [];
const classified = new Set(matching(patterns));
if (paths.some(x => !classified.has(x))) throw new Error(`Unclassified changed paths: ${paths.filter(x => !classified.has(x)).join(", ")}`);
const changedIn = pats => {
  if (!pats.length) return false;
  return git(["diff", "--no-renames", "--name-only", base, "HEAD", "--", ...pats.map(x => `:(glob)${x}`)]).length > 0;
};
const shared = scope === "changed" && changedIn(declaration.gates.shared);
const selected = plan.selections[scope].filter(id => scope === "full" || shared || changedIn(declaration.gates.units[id].inputs));
const head = git(["rev-parse", "HEAD"])[0];
if (process.env.CANDIDATE_SHA && process.env.CANDIDATE_SHA !== head) throw new Error("Candidate SHA changed");
if (!selected.length) {
  console.log(JSON.stringify({ status: "skipped", scope, checks: [], reason: "not-applicable", source_receipts: [{ candidate_sha: head }] }));
  process.exit(0);
}
const results = [];
for (const id of selected) {
  const unit = plan.units.find(x => x.id === id);
  if (!unit || unit.dependencies.length) throw new Error(`Invalid unit: ${id}`);
  const result = spawnSync(unit.command[0], unit.command.slice(1), { stdio: "inherit", timeout: unit.timeout_seconds * 1000 });
  results.push({ id, status: result.status === 0 ? "pass" : "fail" });
  if (result.error || result.status !== 0) { console.log(JSON.stringify({ status: "failed", scope, checks: selected, reason: "validation-failed", source_receipts: [{ candidate_sha: head, results }] })); process.exit(1); }
}
console.log(JSON.stringify({ status: "pass", scope, checks: selected, source_receipts: [{ candidate_sha: head, results }] }));
