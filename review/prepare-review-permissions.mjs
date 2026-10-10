import { appendFileSync, closeSync, lstatSync, openSync, realpathSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

export function preparePermissions(allowedTools, reviewFile) {
  if (/[\r\n"]/.test(allowedTools)) {
    throw new Error("allowed_tools must be one line without double quotes");
  }
  if (reviewFile === "") return allowedTools;
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(reviewFile)) {
    throw new Error("review_file must be a repository-root filename");
  }
  return [allowedTools, `Edit(/${reviewFile})`].filter(Boolean).join(",");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const reviewFile = process.env.REVIEW_FILE ?? "";
  const allowedTools = preparePermissions(
    process.env.ALLOWED_TOOLS ?? "",
    reviewFile,
  );
  if (reviewFile !== "") {
    const workspace = process.env.GITHUB_WORKSPACE;
    if (!workspace || realpathSync(workspace) !== process.cwd()) {
      throw new Error("review_file requires the action to start at GITHUB_WORKSPACE");
    }
    const target = resolve(workspace, reviewFile);
    try {
      closeSync(openSync(target, "wx", 0o600));
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      if (!lstatSync(target).isFile()) {
        throw new Error("review_file target must be a regular file");
      }
    }
  }
  appendFileSync(process.env.GITHUB_OUTPUT, `allowed_tools=${allowedTools}\n`);
}
