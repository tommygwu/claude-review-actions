import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { extractObjectiveContext } from "./objective-context.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const markdown = (...lines) => lines.join("\n");

test("criteria and non-goals stay inside Intent and end at the next heading", () => {
  const context = extractObjectiveContext(markdown(
    "### Acceptance Criteria", "- outside Intent",
    "   # Intent ###", "   ### Acceptance Criteria ###", "- Reject expired invitations.",
    "#### Evidence", "- not another criterion",
    "### Non-goals", "- Change existing sessions.",
    "# Delivery", "### Acceptance Criteria", "- deploy notes",
    "### Non-goals", "- delivery notes",
  ));
  assert.equal(context.source, "structured");
  assert.equal(context.hasStructuredSections, true);
  assert.deepEqual(context.promisedBehaviors, ["Reject expired invitations."]);
  assert.deepEqual(context.nonGoals, ["Change existing sessions."]);
  assert.deepEqual(context.fallbackCandidates, []);
  assert.deepEqual(context.validSections, { promisedBehaviors: true, nonGoals: true });
  assert.deepEqual(context.errors, []);
});

test("empty H1 and H4 headings end Intent and acceptance criteria", () => {
  for (const [body, promisedBehaviors] of [
    ["# Intent\n#\n### Acceptance Criteria\n- Outside Intent.", []],
    ["# Intent\n### Acceptance Criteria\n- Real criterion.\n####\n- Outside criteria.", ["Real criterion."]],
  ]) {
    const context = extractObjectiveContext(body);
    assert.deepEqual(context.promisedBehaviors, promisedBehaviors, body);
    assert.deepEqual(context.nonGoals, [], body);
  }
});

test("quoted, indented, wrong-level and differently cased headings do not create criteria", () => {
  const context = extractObjectiveContext(markdown(
    "# Summary", "Preserve the existing login.",
    "# Intent", "## Acceptance Criteria", "- wrong level",
    "### acceptance criteria", "- wrong case",
    "> ### Acceptance Criteria", "> - quoted example",
    "    ### Acceptance Criteria", "    - indented example",
  ));
  assert.equal(context.source, "fallback");
  assert.equal(context.hasStructuredSections, false);
  assert.deepEqual(context.promisedBehaviors, []);
  assert.deepEqual(context.fallbackCandidates, ["Preserve the existing login."]);
});

test("fenced and commented headings and bullets cannot change the objective", () => {
  const context = extractObjectiveContext(markdown(
    "````markdown", "# Intent", "### Acceptance Criteria", "- backtick example", "```", "# Intent", "````",
    "~~~markdown", "# Intent", "### Acceptance Criteria", "- tilde example", "~~~~",
    "<!--", "# Intent", "### Acceptance Criteria", "- commented example", "-->",
    "# Intent", "### Acceptance Criteria", "- Keep <!-- hidden text -->the real criterion.",
    "<!-- ### Acceptance Criteria -->", "<!-- - hidden bullet -->",
  ).replaceAll("\n", "\r\n"));
  assert.equal(context.source, "structured");
  assert.deepEqual(context.promisedBehaviors, ["Keep the real criterion."]);
  assert.deepEqual(context.errors, []);
});

test("a fenced list-item example cannot create duplicate objective sections", () => {
  for (const [opener, closer] of [["- ```markdown", "  ```"], ["1. ~~~markdown", "   ~~~"]]) {
    const context = extractObjectiveContext(markdown(
      "# Intent", "### Acceptance Criteria", "- Real criterion.",
      opener, "  # Intent", "  ### Acceptance Criteria", "  - Example criterion.", closer,
      "### Non-goals", "- Preserve sessions.",
    ));
    assert.deepEqual(context.promisedBehaviors, ["Real criterion."], opener);
    assert.deepEqual(context.nonGoals, ["Preserve sessions."], opener);
    assert.deepEqual(context.errors, [], opener);
  }
});

test("an HTML comment opener in inline code preserves later criteria and non-goals", () => {
  for (const criterion of ["Reject the literal `<!--` in a title.", "Reject ``<!-- with a ` inside`` in a title."]) {
    const context = extractObjectiveContext(markdown(
      "# Intent", "### Acceptance Criteria", `- ${criterion}`,
      "- Preserve <!-- hidden text -->the audit record.",
      "### Non-goals", "- Preserve sessions.",
    ));
    assert.deepEqual(context.promisedBehaviors, [criterion, "Preserve the audit record."], criterion);
    assert.deepEqual(context.nonGoals, ["Preserve sessions."], criterion);
    assert.deepEqual(context.errors, [], criterion);
  }
  const multiline = extractObjectiveContext(markdown(
    "# Intent", "### Acceptance Criteria", "- Preserve `<!--", "  marker -->` literally.",
    "- Keep later criterion.", "### Non-goals", "- Preserve sessions.",
  ));
  assert.deepEqual(multiline.promisedBehaviors, ["Preserve `<!--\n  marker -->` literally.", "Keep later criterion."]);
  assert.deepEqual(multiline.nonGoals, ["Preserve sessions."]);
  assert.deepEqual(multiline.errors, []);
  const unmatched = extractObjectiveContext(markdown(
    "# Intent", "### Acceptance Criteria", "- Keep an unmatched ` marker. <!-- hidden -->", "",
    "### Non-goals", "- Preserve `sessions`.",
  ));
  assert.deepEqual(unmatched.promisedBehaviors, ["Keep an unmatched ` marker."]);
  assert.deepEqual(unmatched.nonGoals, ["Preserve `sessions`."]);
  const indentedHeading = extractObjectiveContext(markdown(
    "# Intent", "### Acceptance Criteria", "- Preserve an unmatched ` marker.",
    "  ### Non-goals", "  Example ` marker.", "- Preserve sessions.",
  ));
  assert.deepEqual(indentedHeading.promisedBehaviors, ["Preserve an unmatched ` marker."]);
  assert.deepEqual(indentedHeading.nonGoals, ["Preserve sessions."]);
});

test("two multiline code spans preserve the criterion and finish extraction", () => {
  const body = markdown(
    "# Intent", "### Acceptance Criteria", "- Preserve `first", "  span` and `second", "  span` literally.",
    "- Keep later criterion.", "### Non-goals", "- Preserve sessions.",
  );
  const source = `import { extractObjectiveContext } from ${JSON.stringify(path.join(HERE, "objective-context.mjs"))};
    process.stdout.write(JSON.stringify(extractObjectiveContext(${JSON.stringify(body)})));`;
  const result = spawnSync(process.execPath, ["--max-old-space-size=128", "--input-type=module", "-e", source], {
    encoding: "utf8", timeout: 2000,
  });
  assert.equal(result.status, 0, result.error?.message ?? result.stderr);
  const context = JSON.parse(result.stdout);
  assert.deepEqual(context.promisedBehaviors, ["Preserve `first\n  span` and `second\n  span` literally.", "Keep later criterion."]);
  assert.deepEqual(context.nonGoals, ["Preserve sessions."]);
});

test("each top-level bullet retains its continuation and nested items", () => {
  const context = extractObjectiveContext(markdown(
    "# Intent", "### Acceptance Criteria",
    "- Reject expired invitations", "  even when a previous invite exists.",
    "  - Keep the active session.", "    1. Preserve its owner.",
    "* Accept an unexpired invitation.", "+ Record the result.",
    "1. Notify the owner.", "2) Preserve the audit history.",
    "### Non-goals", "- None", "* N/A.", "+ Not applicable", "- None of the existing sessions may change.",
  ));
  assert.deepEqual(context.promisedBehaviors, [
    "Reject expired invitations\n  even when a previous invite exists.\n  - Keep the active session.\n    1. Preserve its owner.",
    "Accept an unexpired invitation.", "Record the result.", "Notify the owner.", "Preserve the audit history.",
  ]);
  assert.deepEqual(context.nonGoals, ["None of the existing sessions may change."]);
  assert.deepEqual(context.validSections, { promisedBehaviors: true, nonGoals: true });
});

test("empty or duplicate criteria suppress a usable Summary fallback", () => {
  for (const [section, error] of [
    ["### Acceptance Criteria\n", "empty-section"],
    ["### Acceptance Criteria\nprose without a bullet", "empty-section"],
    ["### Acceptance Criteria\n- one\n### Acceptance Criteria\n- two", "duplicate-heading"],
  ]) {
    const context = extractObjectiveContext(`# Summary\nLegacy promise.\n# Intent\n${section}`);
    assert.equal(context.source, "structured", section);
    assert.deepEqual(context.promisedBehaviors, [], section);
    assert.deepEqual(context.fallbackCandidates, [], section);
    assert.equal(context.validSections.promisedBehaviors, false, section);
    assert.deepEqual(context.errors, [`Acceptance Criteria:${error}`], section);
  }
});

test("an explicit empty criteria list is valid and still suppresses fallback", () => {
  for (const value of ["None", "N/A.", "not applicable"]) {
    const context = extractObjectiveContext(`# Summary\nLegacy promise.\n# Intent\n### Acceptance Criteria\n- ${value}`);
    assert.equal(context.source, "structured");
    assert.equal(context.validSections.promisedBehaviors, true);
    assert.deepEqual(context.promisedBehaviors, []);
    assert.deepEqual(context.fallbackCandidates, []);
    assert.deepEqual(context.errors, []);
  }
});

test("duplicate or empty non-goals do not discard valid acceptance criteria", () => {
  for (const [section, error] of [
    ["### Non-goals\n", "empty-section"],
    ["### Non-goals\n- one\n### Non-goals\n- two", "duplicate-heading"],
  ]) {
    const context = extractObjectiveContext(`# Intent\n### Acceptance Criteria\n- Reject expired invitations.\n${section}`);
    assert.deepEqual(context.promisedBehaviors, ["Reject expired invitations."]);
    assert.deepEqual(context.nonGoals, []);
    assert.deepEqual(context.validSections, { promisedBehaviors: true, nonGoals: false });
    assert.deepEqual(context.errors, [`Non-goals:${error}`]);
  }
});

test("duplicate Intent sections emit neither structured objectives nor fallback", () => {
  const context = extractObjectiveContext(markdown(
    "# Summary", "Legacy promise.",
    "# Intent", "### Acceptance Criteria", "- first promise", "### Non-goals", "- first exclusion",
    "# Intent", "### Acceptance Criteria", "- second promise",
  ));
  assert.equal(context.source, "structured");
  assert.deepEqual(context.promisedBehaviors, []);
  assert.deepEqual(context.nonGoals, []);
  assert.deepEqual(context.fallbackCandidates, []);
  assert.deepEqual(context.validSections, { promisedBehaviors: false, nonGoals: false });
  assert.ok(context.errors.includes("Intent:duplicate-heading"));
});

test("Summary fallback ends before subheadings and keeps exclusions separate", () => {
  const context = extractObjectiveContext(markdown(
    "# Summary", "Reject expired invitations.", "- Preserve active sessions.",
    "### Why", "This explanation is not another promise.",
    "# Intent", "### Non-goals", "- Change the audit history.",
    "# Delivery", "Release the parser.",
  ));
  assert.equal(context.source, "fallback");
  assert.deepEqual(context.promisedBehaviors, []);
  assert.deepEqual(context.fallbackCandidates, ["Reject expired invitations.", "Preserve active sessions."]);
  assert.deepEqual(context.nonGoals, ["Change the audit history."]);
});

test("a single legacy Why or Why? is a fallback only when Summary is absent", () => {
  for (const heading of ["Why", "Why?"]) {
    for (const prefix of ["", "# Intent\n"]) {
      const context = extractObjectiveContext(`${prefix}### ${heading}\nReject expired invitations.\n### How\nImplementation detail.`);
      assert.equal(context.source, "fallback");
      assert.deepEqual(context.fallbackCandidates, ["Reject expired invitations."]);
    }
  }
  const context = extractObjectiveContext("# Summary\n### Why\nMust not replace the empty Summary.");
  assert.equal(context.source, "none");
  assert.deepEqual(context.fallbackCandidates, []);
});

test("ambiguous fallback sections do not produce objectives", () => {
  for (const body of [
    "# Summary\nfirst\n# Summary\nsecond",
    "### Why\nfirst\n### Why?\nsecond",
  ]) {
    const context = extractObjectiveContext(body);
    assert.equal(context.source, "none");
    assert.deepEqual(context.fallbackCandidates, []);
  }
});

test("Why under Delivery cannot supply a fallback objective", () => {
  const context = extractObjectiveContext("# Delivery\n### Why\n- This deployment note is not a task promise.");
  assert.equal(context.source, "none");
  assert.deepEqual(context.fallbackCandidates, []);
});

test("an absent body or non-goals alone never invent a positive objective", () => {
  for (const body of ["", "\n\r\n", "# Intent\n### Non-goals\n- Change existing sessions."]) {
    const context = extractObjectiveContext(body);
    assert.equal(context.source, "none");
    assert.deepEqual(context.promisedBehaviors, []);
    assert.deepEqual(context.fallbackCandidates, []);
    assert.deepEqual(context.nonGoals, body.includes("Non-goals") ? ["Change existing sessions."] : []);
  }
});

function withDirectory(run) {
  const directory = mkdtempSync(path.join(tmpdir(), "objective-context-"));
  try {
    return run(directory);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

function readOutputs(text) {
  const lines = text.trimEnd().split("\n");
  const outputs = {};
  for (let index = 0; index < lines.length; index += 1) {
    const match = lines[index].match(/^(\w+)<<(.+)$/);
    assert.ok(match, `invalid output record: ${lines[index]}`);
    const end = lines.indexOf(match[2], index + 1);
    assert.ok(end > index, `unterminated output: ${match[1]}`);
    assert.equal(Object.hasOwn(outputs, match[1]), false, `duplicate output: ${match[1]}`);
    outputs[match[1]] = lines.slice(index + 1, end).join("\n");
    index = end;
  }
  return outputs;
}

function interpolate(text, context) {
  return text.replace(/\$\{\{\s*([^}]+?)\s*\}\}/g, (_, expression) => {
    assert.ok(Object.hasOwn(context, expression), `unresolved action expression: ${expression}`);
    return context[expression];
  });
}

test("the composite action passes shell-like body text as data and exposes all CLI outputs", () => {
  const action = readFileSync(path.join(HERE, "action.yml"), "utf8");
  const command = action.match(/^      run: (.+)$/m)?.[1];
  assert.ok(command, "the composite action must run its extractor");
  withDirectory((directory) => {
    const body = markdown(
      "# Intent", "### Acceptance Criteria",
      '- Preserve $(touch injected) and `touch injected-too` literally; "quotes" and \'apostrophes\'.',
      "  source=none", "  context<<EOF", "  EOF", "  objective_deadbeef", "  café 🧪",
      "### Non-goals", "- Change the $PATH value.",
    );
    const expressions = { "github.action_path": HERE, "inputs.pr_body": body };
    const output = path.join(directory, "output");
    writeFileSync(output, "previous=untouched\n");
    const env = { ...process.env, GITHUB_OUTPUT: output, PATH: `${path.dirname(process.execPath)}:${process.env.PATH}` };
    delete env.PR_BODY;
    delete env.ACTION_PATH;
    for (const [, name, value] of action.matchAll(/^        ([A-Z_]+): (.+)$/gm)) {
      env[name] = interpolate(value, expressions);
    }
    const result = spawnSync("bash", ["-c", interpolate(command, expressions)], { cwd: directory, env, encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    const contents = readFileSync(output, "utf8");
    assert.ok(contents.startsWith("previous=untouched\n"));
    const emitted = readOutputs(contents.slice("previous=untouched\n".length));
    assert.deepEqual(Object.keys(emitted).sort(), ["context", "objective", "source"]);
    for (const [name, value] of Object.entries(emitted)) expressions[`steps.extract.outputs.${name}`] = value;
    const outputs = {};
    for (const name of ["context", "objective", "source"]) {
      const section = action.match(new RegExp(`^  ${name}:\\n((?:    .+\\n)+)`, "m"))?.[1];
      const value = section?.match(/^    value: (.+)$/m)?.[1];
      assert.ok(value, `missing public output: ${name}`);
      outputs[name] = interpolate(value, expressions);
    }
    const context = JSON.parse(outputs.context);
    assert.equal(outputs.source, "structured");
    assert.equal(context.source, outputs.source);
    assert.deepEqual(context.promisedBehaviors, [body.split("### Acceptance Criteria\n- ")[1].split("\n### Non-goals")[0]]);
    assert.deepEqual(context.nonGoals, ["Change the $PATH value."]);
    assert.ok(outputs.objective.includes(context.promisedBehaviors[0]));
    assert.ok(outputs.objective.includes(context.nonGoals[0]));
    assert.deepEqual(readdirSync(directory), ["output"]);
  });
});

test("the CLI defaults a missing PR_BODY to no objective and fails output I/O errors", () => {
  withDirectory((directory) => {
    const env = { ...process.env, GITHUB_OUTPUT: path.join(directory, "output") };
    delete env.PR_BODY;
    const result = spawnSync(process.execPath, [path.join(HERE, "objective-context.mjs")], { env, encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    const outputs = readOutputs(readFileSync(env.GITHUB_OUTPUT, "utf8"));
    assert.equal(outputs.source, "none");
    assert.deepEqual(JSON.parse(outputs.context).fallbackCandidates, []);
    for (const output of [undefined, directory]) {
      const invalidEnv = { ...env };
      if (output === undefined) delete invalidEnv.GITHUB_OUTPUT;
      else invalidEnv.GITHUB_OUTPUT = output;
      const failure = spawnSync(process.execPath, [path.join(HERE, "objective-context.mjs")], { env: invalidEnv, encoding: "utf8" });
      assert.notEqual(failure.status, 0);
    }
  });
});
