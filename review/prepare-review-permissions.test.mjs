import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function withOutput(run) {
  const directory = mkdtempSync(path.join(tmpdir(), "review-permissions-"));
  const output = path.join(directory, "output");
  writeFileSync(output, "existing=untouched\n");
  try {
    return run(output, directory);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

function prepare(inputs, output, { cwd = path.dirname(output), workspace = cwd } = {}) {
  const env = { ...process.env, GITHUB_OUTPUT: output };
  delete env.ALLOWED_TOOLS;
  delete env.REVIEW_FILE;
  delete env.GITHUB_WORKSPACE;
  if (workspace !== null) env.GITHUB_WORKSPACE = workspace;
  return spawnSync(process.execPath, [path.join(HERE, "prepare-review-permissions.mjs")], {
    cwd,
    env: { ...env, ...inputs },
    encoding: "utf8",
  });
}

test("omitting review_file preserves caller permissions byte for byte", () => {
  for (const reviewFile of [undefined, ""]) {
    withOutput((output, directory) => {
      const allowedTools = " Bash(gh pr diff:*),Bash(cat:*),Edit(custom.md) ";
      const inputs = { ALLOWED_TOOLS: allowedTools };
      if (reviewFile !== undefined) inputs.REVIEW_FILE = reviewFile;
      const result = prepare(inputs, output, { workspace: null });
      assert.equal(result.status, 0, result.stderr);
      assert.equal(readFileSync(output, "utf8"), `existing=untouched\nallowed_tools=${allowedTools}\n`);
      assert.deepEqual(readdirSync(directory), ["output"]);
    });
  }
});

test("root review filename produces an anchored rule without broadening caller tools", () => {
  for (const filename of ["review.md", "Review-2_body.v1.md"]) {
    withOutput((output) => {
      const result = prepare({ ALLOWED_TOOLS: "Bash(cat:*)", REVIEW_FILE: filename }, output);
      assert.equal(result.status, 0, result.stderr);
      assert.equal(readFileSync(output, "utf8"), `existing=untouched\nallowed_tools=Bash(cat:*),Edit(/${filename})\n`);
    });
  }
});

test("a review file with no extra tools emits only its one-file rule", () => {
  withOutput((output) => {
    const result = prepare({ REVIEW_FILE: "review.md" }, output);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(readFileSync(output, "utf8"), "existing=untouched\nallowed_tools=Edit(/review.md)\n");
  });
});

test("a missing review file is reserved as an empty private regular file", () => {
  withOutput((output, directory) => {
    const result = prepare({ REVIEW_FILE: "review.md" }, output);
    assert.equal(result.status, 0, result.stderr);
    const target = path.join(directory, "review.md");
    const metadata = lstatSync(target);
    assert.ok(metadata.isFile());
    assert.equal(metadata.mode & 0o777, 0o600);
    assert.equal(readFileSync(target, "utf8"), "");
  });
});

test("an existing regular review file keeps its content and mode", () => {
  withOutput((output, directory) => {
    const target = path.join(directory, "review.md");
    writeFileSync(target, "existing review body\n", { mode: 0o640 });
    const before = lstatSync(target);
    const result = prepare({ REVIEW_FILE: "review.md" }, output);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(readFileSync(target, "utf8"), "existing review body\n");
    assert.equal(lstatSync(target).mode, before.mode);
    assert.equal(lstatSync(target).ino, before.ino);
    assert.equal(readFileSync(output, "utf8"), "existing=untouched\nallowed_tools=Edit(/review.md)\n");
  });
});

test("a review-file directory receives no writable subtree grant", () => {
  withOutput((output, directory) => {
    const target = path.join(directory, "review.md");
    mkdirSync(target);
    const child = path.join(target, "sensitive.txt");
    writeFileSync(child, "keep this child\n");
    const result = prepare({ REVIEW_FILE: "review.md" }, output);
    assert.notEqual(result.status, 0);
    assert.equal(readFileSync(output, "utf8"), "existing=untouched\n");
    assert.equal(readFileSync(child, "utf8"), "keep this child\n");
  });
});

test("review-file symlinks receive no grant and leave their targets untouched", () => {
  for (const kind of ["file", "directory", "missing"]) {
    withOutput((output, directory) => {
      const destination = path.join(directory, "destination");
      if (kind === "file") writeFileSync(destination, "keep this target\n");
      if (kind === "directory") mkdirSync(destination);
      const target = path.join(directory, "review.md");
      symlinkSync(destination, target);
      const result = prepare({ REVIEW_FILE: "review.md" }, output);
      assert.notEqual(result.status, 0, kind);
      assert.equal(readFileSync(output, "utf8"), "existing=untouched\n", kind);
      assert.ok(lstatSync(target).isSymbolicLink(), kind);
      if (kind === "file") assert.equal(readFileSync(destination, "utf8"), "keep this target\n");
      if (kind === "directory") assert.deepEqual(readdirSync(destination), []);
      if (kind === "missing") assert.equal(existsSync(destination), false);
    });
  }
});

test("a nested startup directory is rejected before reserving or granting a review file", () => {
  withOutput((output, directory) => {
    const nested = path.join(directory, "nested");
    mkdirSync(nested);
    const result = prepare({ REVIEW_FILE: "review.md" }, output, { cwd: nested, workspace: directory });
    assert.notEqual(result.status, 0);
    assert.equal(readFileSync(output, "utf8"), "existing=untouched\n");
    assert.equal(existsSync(path.join(directory, "review.md")), false);
    assert.equal(existsSync(path.join(nested, "review.md")), false);
  });
});

test("opt-in requires GITHUB_WORKSPACE before reserving or granting a review file", () => {
  withOutput((output, directory) => {
    const result = prepare({ REVIEW_FILE: "review.md" }, output, { workspace: null });
    assert.notEqual(result.status, 0);
    assert.equal(readFileSync(output, "utf8"), "existing=untouched\n");
    assert.equal(existsSync(path.join(directory, "review.md")), false);
  });
});

test("a workspace symlink to the startup root is accepted by real path", () => {
  withOutput((output, directory) => {
    const workspace = path.join(directory, "workspace-link");
    symlinkSync(directory, workspace);
    const result = prepare({ REVIEW_FILE: "review.md" }, output, { workspace });
    assert.equal(result.status, 0, result.stderr);
    assert.ok(lstatSync(path.join(directory, "review.md")).isFile());
    assert.equal(readFileSync(output, "utf8"), "existing=untouched\nallowed_tools=Edit(/review.md)\n");
  });
});

test("invalid review paths and rule injection fail before writing any action output", () => {
  for (const filename of ["../review.md", "nested/review.md", "/review.md", "nested\\review.md", ".", "..", ".review.md", "review*.md", "review?.md", "review[1].md", "review.md),Bash(*)", 'review.md" --dangerously-skip-permissions', "review.md\nallowed_tools=Bash(*)", "review.md\rallowed_tools=Bash(*)", "review body.md"]) {
    withOutput((output) => {
      const result = prepare({ ALLOWED_TOOLS: "Bash(cat:*)", REVIEW_FILE: filename }, output);
      assert.notEqual(result.status, 0, filename);
      assert.equal(readFileSync(output, "utf8"), "existing=untouched\n", filename);
    });
  }
});

test("tool argument and action-output injection fail even when review_file is omitted", () => {
  for (const allowedTools of ['Bash(cat:*)" --dangerously-skip-permissions "', "Bash(cat:*)\nallowed_tools=Bash(*)", "Bash(cat:*)\rallowed_tools=Bash(*)"]) {
    withOutput((output) => {
      const result = prepare({ ALLOWED_TOOLS: allowedTools }, output);
      assert.notEqual(result.status, 0, allowedTools);
      assert.equal(readFileSync(output, "utf8"), "existing=untouched\n", allowedTools);
    });
  }
});

function interpolate(text, context) {
  return text.replace(/\$\{\{\s*([^}]+?)\s*\}\}/g, (_, expression) => {
    assert.ok(Object.hasOwn(context, expression), `unresolved action expression: ${expression}`);
    return context[expression];
  });
}

test("the action executes preparation and forwards its output as one reviewer argument", () => {
  const action = readFileSync(path.join(HERE, "action.yml"), "utf8");
  const steps = [...action.matchAll(/^    - id: ([^\n]+)\n([\s\S]*?)(?=^    - id: |$(?![\s\S]))/gm)];
  const reviewIndex = steps.findIndex((step) => /^      uses: anthropics\/claude-code-action@/m.test(step[2]));
  assert.ok(reviewIndex > 0, "the reviewer must have a preceding preparation step");
  const reviewer = steps[reviewIndex];
  const preparation = steps.slice(0, reviewIndex).find((step) => /^      run: .*prepare-review-permissions\.mjs/m.test(step[2]));
  assert.ok(preparation, "the action must run the permission CLI before the reviewer");
  const command = preparation[2].match(/^      run: (.+)$/m)?.[1];
  const argumentText = reviewer[2].match(/^        claude_args: >-\n((?:          .+\n)+)/m)?.[1];
  assert.ok(argumentText, "the actual reviewer must receive claude_args");

  for (const reviewFile of ["review.md", ""]) {
    withOutput((output) => {
      const allowedTools = "Bash(gh pr diff:*),Bash(cat:*),Edit(custom.md)";
      const context = {
        "github.action_path": HERE,
        "inputs.allowed_tools": allowedTools,
        "inputs.review_file": reviewFile,
      };
      const env = { ...process.env, GITHUB_OUTPUT: output, GITHUB_WORKSPACE: path.dirname(output), PATH: `${path.dirname(process.execPath)}:${process.env.PATH}` };
      for (const [, name, value] of preparation[2].matchAll(/^        ([A-Z_]+): (.+)$/gm)) {
        env[name] = interpolate(value, context);
      }
      const result = spawnSync("bash", ["-c", interpolate(command, context)], { cwd: path.dirname(output), env, encoding: "utf8" });
      assert.equal(result.status, 0, result.stderr);
      const emitted = readFileSync(output, "utf8").split("\n").find((line) => line.startsWith("allowed_tools="));
      assert.ok(emitted);
      context[`steps.${preparation[1]}.outputs.allowed_tools`] = emitted.slice("allowed_tools=".length);
      const args = interpolate(argumentText.trim().replace(/\s*\n\s*/g, " "), context);
      const parsed = [...args.matchAll(/"([^"]*)"|([^\s"]+)/g)].map((match) => match[1] ?? match[2]);
      assert.equal(parsed.filter((arg) => arg === "--allowed-tools").length, 1);
      assert.equal(parsed[parsed.indexOf("--allowed-tools") + 1], reviewFile ? `${allowedTools},Edit(/review.md)` : allowedTools);
    });
  }
});
