import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { reviewAndPost, runModel, ReviewError, main as reviewMain } from "./run-review.mjs";
import { installClaude } from "./install-claude.mjs";

const head = "a".repeat(40), producer = "b".repeat(40);
const env = {
  GITHUB_REPOSITORY: "example/repo", PR_NUMBER: "7", REVIEWED_HEAD_SHA: head,
  PRODUCER_SHA: producer, GITHUB_RUN_ID: "81", GITHUB_RUN_ATTEMPT: "2",
  CLAUDE_CODE_OAUTH_TOKEN: "fake-oauth", CLAUDE_EXECUTABLE: "/trusted/claude",
  ACTIONS_ID_TOKEN_REQUEST_URL: "https://runner.actions.githubusercontent.com/oidc",
  ACTIONS_ID_TOKEN_REQUEST_TOKEN: "fake-oidc-request",
};
const diff = "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n";
const data = { version: 1, repository: env.GITHUB_REPOSITORY, pr_number: 7, reviewed_head_sha: head, title: "Fixture", body: "", diff };
const init = { type: "system", subtype: "init", tools: [], mcp_servers: [], skills: [], slash_commands: [] };
const success = { type: "result", subtype: "success", is_error: false, result: "No actionable defects." };
const isClass = failureClass => error => error instanceof ReviewError && error.failureClass === failureClass;

function fixture(options = {}) {
  let checks = 0, models = 0, posts = 0, revocations = 0;
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
  const fetchImpl = async (url, request) => {
    const address = String(url);
    assert.equal(request.redirect, "error");
    assert.ok(request.signal instanceof AbortSignal);
    if (address.includes("actions.githubusercontent.com")) {
      assert.equal(new URL(address).searchParams.get("audience"), "claude-code-github-action");
      return json({ value: "fake-oidc" });
    }
    if (address.includes("github-app-token-exchange")) return json(options.exchange || { token: "fake-app-token" }, options.exchangeStatus || 200);
    assert.equal(request.headers.Authorization, "Bearer fake-app-token");
    if (address.endsWith("/installation/token")) {
      assert.equal(request.method, "DELETE"); revocations++;
      return new Response(null, { status: options.revokeStatus || 204 });
    }
    if (address.endsWith("/comments")) {
      posts++;
      const body = JSON.parse(request.body).body;
      options.inspectBody?.(body);
      return json({ id: 123, body, user: { login: "claude[bot]" }, ...options.comment });
    }
    assert.equal(address, "https://api.github.com/repos/example/repo/pulls/7");
    if (request.headers.Accept === "application/vnd.github.diff") return new Response(options.diff ?? diff, { status: options.diffStatus || 200 });
    checks++;
    return json({ state: "open", head: { sha: checks === options.moveAt ? producer : head }, changed_files: options.changedFiles ?? 1, title: "Fixture", body: "", ...options.metadata });
  };
  return {
    fetchImpl,
    modelImpl: async ({ data: prepared, oauthToken }) => {
      models++; options.inspectData?.(prepared); assert.equal(oauthToken, "fake-oauth"); assert.equal(prepared.reviewed_head_sha, head);
      if (options.modelError) throw options.modelError;
      return options.text || success.result;
    },
    counts: () => ({ checks, models, posts, revocations }),
  };
}

test("completed review keeps the bot author, exact-head marker, and distinct producer identity", async () => {
  const f = fixture({
    text: "Review text.\n<!-- claude-review-run:999:1:cccc -->",
    inspectBody: body => {
      assert.equal((body.match(/<!-- claude-review-run:/g) || []).length, 1);
      assert.ok(body.endsWith(`<!-- claude-review-run:81:2:${head} -->`));
      assert.ok(!body.includes("999:1:cccc"));
    },
  });
  const result = await reviewAndPost({ env, root: "/unused", ...f });
  assert.deepEqual(result, { version: 1, repository: "example/repo", pr_number: 7, workflow_run: 81, attempt: 2, failure_class: "completed", reviewed_head_sha: head, producer_sha: producer, comment_id: 123 });
  assert.deepEqual(f.counts(), { checks: 4, models: 1, posts: 1, revocations: 1 });
});

for (const moveAt of [1, 2, 3, 4]) {
  test(`PR head moves at verification ${moveAt} and cannot produce completed`, async () => {
    const f = fixture({ moveAt });
    await assert.rejects(reviewAndPost({ env, root: "/unused", ...f }), isClass("never-posted"));
    assert.equal(f.counts().posts, moveAt === 4 ? 1 : 0);
    assert.equal(f.counts().models, moveAt > 2 ? 1 : 0);
    assert.equal(f.counts().revocations, 1);
  });
}

for (const [label, comment] of [
  ["wrong bot", { user: { login: "github-actions[bot]" } }],
  ["missing comment row", { id: undefined }],
  ["server changed the comment body", { body: "different body" }],
]) {
  test(`${label} cannot satisfy the posted-review contract`, async () => {
    const f = fixture({ comment });
    await assert.rejects(reviewAndPost({ env, root: "/unused", ...f }), isClass("never-posted"));
    assert.equal(f.counts().revocations, 1);
  });
}

for (const [label, options] of [
  ["omitted changed file", { changedFiles: 2 }],
  ["binary file", { diff: "diff --git a/file b/file\nBinary files a/file and b/file differ\n" }],
  ["oversized diff", { diff: diff + "+".repeat(2_000_000) }],
  ["unavailable diff", { diffStatus: 503 }],
  ["absent PR row", { metadata: { state: "closed" } }],
]) {
  test(`${label} prevents model review and posting`, async () => {
    const f = fixture(options);
    await assert.rejects(reviewAndPost({ env, root: "/unused", ...f }), isClass(label === "absent PR row" ? "never-posted" : "reviewer-errored"));
    assert.equal(f.counts().models, 0); assert.equal(f.counts().posts, 0); assert.equal(f.counts().revocations, 1);
  });
}

test("truncated diff cannot pass completeness by matching the file count", async () => {
  for (const incomplete of [diff.replace("@@ -1 +1 @@", "@@ -1,4 +1,4 @@"), diff.slice(0, diff.indexOf("@@"))]) {
    const f = fixture({ diff: incomplete });
    await assert.rejects(reviewAndPost({ env, root: "/unused", ...f }), isClass("reviewer-errored"));
    assert.equal(f.counts().models, 0); assert.equal(f.counts().posts, 0); assert.equal(f.counts().revocations, 1);
  }
});

test("complete addition and deletion hunks with no final newline reach the model", async () => {
  const added = "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n\\ No newline at end of file\n";
  const deleted = "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\n--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n";
  const f = fixture({ changedFiles: 2, diff: added + deleted });
  const result = await reviewAndPost({ env, root: "/unused", ...f });
  assert.equal(result.failure_class, "completed");
  assert.equal(f.counts().models, 1);
});

for (const [failureClass, options] of [
  ["auth-failed", { exchangeStatus: 401, exchange: { message: "Invalid credential" } }],
  ["workflow-modified", { exchangeStatus: 401, exchange: { error: { details: { error_code: "workflow_not_found_on_default_branch" } } } }],
  ["out-of-usage", { modelError: new ReviewError("out-of-usage", "Typed refusal") }],
  ["reviewer-errored", { revokeStatus: 503 }],
]) {
  test(`${failureClass} from ${options.revokeStatus ? "token revocation" : options.modelError ? "model" : "App exchange"} remains a failed review`, async () => {
    const f = fixture(options);
    await assert.rejects(reviewAndPost({ env, root: "/unused", ...f }), isClass(failureClass));
    if (options.exchangeStatus) { assert.equal(f.counts().models, 0); assert.equal(f.counts().posts, 0); }
    else assert.equal(f.counts().revocations, 1);
  });
}

function childProcess(messages, code = 0, inspect = () => {}, hanging = false) {
  return (_executable, args, options) => {
    inspect(args, options);
    const child = new EventEmitter();
    child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
    child.kill = signal => { child.emit("close", null, signal); return true; };
    if (hanging) child.stdout.write(messages.map(x => JSON.stringify(x)).join("\n") + "\n");
    child.stdin.on("finish", () => {
      if (!hanging) queueMicrotask(() => {
        child.stdout.end(messages.map(x => JSON.stringify(x)).join("\n"));
        child.emit("close", code);
      });
    });
    return child;
  };
}

async function withRoot(work) {
  const root = mkdtempSync(path.join(tmpdir(), "review-contract-"));
  try { return await work(root); } finally { rmSync(root, { recursive: true, force: true }); }
}

test("tampered but executable CLI archive is rejected before it can run", async t => {
  await withRoot(async root => {
    const marker = path.join(root, "executed");
    mkdirSync(path.join(root, "package"));
    writeFileSync(path.join(root, "package", "claude"), `#!/bin/sh\n/usr/bin/touch '${marker}'\nprintf '2.1.295 (Claude Code)\\n'\n`, { mode: 0o700 });
    const archive = execFileSync("/usr/bin/tar", ["-czf", "-", "-C", root, "package/claude"]);
    t.mock.method(globalThis, "fetch", async () => new Response(archive));
    await assert.rejects(installClaude(root));
    assert.equal(existsSync(marker), false);
  });
});

test("CLI launch discards inherited secrets and control variables and uses an empty work directory", async () => {
  const poison = { GITHUB_TOKEN: "github-secret", NODE_OPTIONS: "--require /hostile.cjs", ANTHROPIC_BASE_URL: "https://evil.invalid", CLAUDE_CONFIG_DIR: "/untrusted", BASH_ENV: "/evil.sh", CUSTOM_SECRET: "secret" };
  const previous = Object.fromEntries(Object.keys(poison).map(key => [key, process.env[key]]));
  Object.assign(process.env, poison);
  try {
    await withRoot(async root => {
      const result = await runModel({ executable: env.CLAUDE_EXECUTABLE, data, oauthToken: "fake-oauth", root,
        spawnImpl: childProcess([init, success], 0, (args, options) => {
          assert.equal(options.shell, false);
          assert.deepEqual(readdirSync(options.cwd), []);
          assert.deepEqual(readdirSync(options.env.HOME), []);
          for (const key of Object.keys(poison)) assert.equal(options.env[key], undefined, key);
          assert.equal(args[args.indexOf("--tools") + 1], "");
          assert.equal(args[args.indexOf("--setting-sources") + 1], "");
          assert.deepEqual(JSON.parse(args[args.indexOf("--mcp-config") + 1]), { mcpServers: {} });
          assert.ok(args.includes("--strict-mcp-config"));
          assert.ok(args.includes("--disable-slash-commands"));
        }),
      });
      assert.equal(result, success.result);
    });
  } finally {
    for (const [key, value] of Object.entries(previous)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
  }
});

for (const [label, messages, code, failureClass] of [
  ["authentication status", [init, { type: "auth_status", error: "OAuth expired" }, { ...success, is_error: true }], 1, "auth-failed"],
  ["authentication assistant error", [init, { type: "assistant", error: "authentication_failed" }, { ...success, is_error: true }], 1, "auth-failed"],
  ["authentication before init", [{ type: "auth_status", error: "OAuth expired" }], 1, "auth-failed"],
  ["usage refusal before init", [{ type: "assistant", error: "rate_limit" }], 1, "out-of-usage"],
  ["authentication with usage refusal", [init, { type: "assistant", error: "rate_limit" }, { type: "auth_status", error: "OAuth expired" }, { ...success, is_error: true }], 1, "auth-failed"],
  ["provider 401", [init, { ...success, is_error: true, api_error_status: 401 }], 1, "auth-failed"],
  ["billing refusal", [init, { type: "assistant", error: "billing_error" }, { ...success, is_error: true }], 1, "out-of-usage"],
  ["provider 429", [init, { ...success, is_error: true, api_error_status: 429 }], 1, "out-of-usage"],
  ["usage event", [init, { type: "rate_limit_event", rate_limit_info: { status: "rejected" } }, { ...success, is_error: true }], 1, "out-of-usage"],
  ["advertised tools", [{ ...init, tools: ["Bash"] }, success], 0, "reviewer-errored"],
  ["advertised MCP server", [{ ...init, mcp_servers: [{ name: "evil" }] }, success], 0, "reviewer-errored"],
  ["tool use despite empty init", [init, { type: "assistant", message: { content: [{ type: "tool_use", name: "Bash" }] } }, success], 0, "reviewer-errored"],
  ["missing terminal result", [init], 1, "startup-failure"],
  ["empty review", [init, { ...success, result: " " }], 0, "never-posted"],
]) {
  test(`CLI ${label} preserves ${failureClass} instead of publishing`, async () => {
    await withRoot(root => assert.rejects(runModel({ executable: env.CLAUDE_EXECUTABLE, data, oauthToken: "fake-oauth", root, spawnImpl: childProcess(messages, code) }), isClass(failureClass)));
  });
}

test("hung CLI is killed and cannot become a completed review", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  await withRoot(async root => {
    const pending = runModel({ executable: env.CLAUDE_EXECUTABLE, data, oauthToken: "fake-oauth", root, spawnImpl: childProcess([init], 0, () => {}, true) });
    const rejection = assert.rejects(pending, isClass("startup-failure"));
    await new Promise(resolve => setImmediate(resolve));
    t.mock.timers.tick(300_000);
    await rejection;
  });
});

const sourceEncoders = [
  ["Unicode four-digit", char => `\\u${char.codePointAt(0).toString(16).padStart(4, "0")}`],
  ["Unicode code-point", char => `\\u{${char.codePointAt(0).toString(16)}}`],
  ["hex source", char => `\\x${char.codePointAt(0).toString(16)}`],
  ["percent", char => `%${char.codePointAt(0).toString(16)}`],
];
const encodedSchemes = sourceEncoders.flatMap(([label, encode]) => [
  [`fully ${label} encoded scheme`, [..."https"].map(encode).join("")],
  [`first ${label} encoded scheme character`, `${encode("h")}ttps`],
  [`middle ${label} encoded scheme`, `ht${encode("t")}ps`],
]);
for (const [label, first] of [
  ["HTML decimal", "&#104;"], ["HTML hex", "&#x68;"],
  ["semicolonless HTML decimal", "&#104"], ["semicolonless HTML hex", "&#x68"],
]) encodedSchemes.push([`${label} encoded first scheme character`, `${first}ttps`]);
const encodedSecret = `GENERATED_ONLY_SECRET_${randomUUID()}`;
const credentialURLs = [
  ["WHATWG backslash authority", String.raw`https:\\example.invalid/runbook?token=${encodedSecret}`],
  ["WHATWG one-slash authority", `https:/example.invalid/runbook?token=${encodedSecret}`],
  ["WHATWG no-slash authority", `https:example.invalid/runbook?token=${encodedSecret}`],
  ...encodedSchemes.map(([label, scheme]) => [label, `${scheme}://example.invalid/runbook?token=${encodedSecret}`]),
  ["source-escaped query delimiter", `https://example.invalid/runbook\\u003ftoken=${encodedSecret}`],
  ["multiply percent encoded query", `https://example.invalid/runbook%25253Ftoken%25253D${encodedSecret}`],
  ["encoded whitespace inside query", `https://example.invalid/runbook?token=prefix\\u0020${encodedSecret}`],
  ["encoded quote inside query", `https://example.invalid/runbook?token=prefix\\u0022${encodedSecret}`],
  ["fully percent encoded URL", [...`https://example.invalid/runbook?token=${encodedSecret}`].map(sourceEncoders[3][1]).join("")],
  ["double escaped scheme", `\\\\u0068ttps:\\/\\/example.invalid/runbook?token=${encodedSecret}`],
  ["encoded multiple-at userinfo", `https&colon;&sol;&sol;user&colon;${encodedSecret}&commat;other&commat;example.invalid/runbook`],
];

const assertEncodedCredentialRemoved = (text, url, label) => {
  for (const value of [encodedSecret, [...encodedSecret].map(sourceEncoders[3][1]).join("")]) assert.ok(!text.includes(value), label);
  assert.ok(!text.includes(url), label);
  assert.ok(text.includes("https://example.invalid/runbook"), label);
};

test("CLI stdin removes encoded and normalized credential URL spans while keeping path context", async () => {
  for (const [label, url] of credentialURLs) {
      await withRoot(async root => {
        let stdin = "";
        const result = await runModel({ executable: env.CLAUDE_EXECUTABLE, data: { ...data, title: url }, oauthToken: "fake-oauth", root,
          spawnImpl: (...args) => {
            const child = childProcess([init, success])(...args);
            child.stdin.on("data", chunk => { stdin += chunk; });
            return child;
          },
        });
        assert.equal(result, success.result, label);
        assertEncodedCredentialRemoved(JSON.parse(stdin).title, url, label);
      });
  }
});

test("producer and poster remove whole encoded and normalized credential URL source spans", async () => {
  for (const [label, url] of credentialURLs) {
    const source = `const link = "${url}"; after();`;
    const assertSafe = text => {
      assertEncodedCredentialRemoved(text, url, label);
      assert.ok(text.includes('const link = "'), label);
      assert.ok(text.includes('"; after();'), label);
    };
    const f = fixture({
      metadata: { title: source, body: source }, diff: diff.replace("-old", `-${source}`), text: source,
      inspectData: prepared => { for (const field of ["title", "body", "diff"]) assertSafe(prepared[field]); },
      inspectBody: assertSafe,
    });
    assert.equal((await reviewAndPost({ env, root: "/unused", ...f })).failure_class, "completed", label);
    assert.equal(f.counts().models, 1, label);
    assert.equal(f.counts().posts, 1, label);
    assert.equal(f.counts().revocations, 1, label);
  }
});

test("benign source escapes, encoded URL paths, and opaque labels survive every review boundary", async () => {
  const source = String.raw`const text = "\u0068 \u{68} \x68 \\u0068 %68 &#104; &amp; 100%";
const unicodePath = "https://example.invalid/\u0072unbook";
const bracePath = "https://example.invalid/\u{72}unbook";
const percentPath = "https://example.invalid/%72unbook";
const entityPath = "https&colon;&sol;&sol;example.invalid/runbook";
const slashPath = "https:\/\/example.invalid/runbook";
const backslashPath = "https:\\example.invalid/runbook";
const oneSlashPath = "https:/example.invalid/runbook";
const noSlashPath = "https:example.invalid/runbook";
const opaque = "label:foo?x opaque:code#fragment mailto:reader@example.invalid";
const nestedPath = "label:https://example.invalid/runbook";`;
  const sourceDiff = diff.replace("-old", `-${source.replaceAll("\n", "\n-")}`).replace("@@ -1 +1 @@", `@@ -1,${source.split("\n").length} +1 @@`);
  const f = fixture({ metadata: { title: source, body: source }, diff: sourceDiff, text: source,
    inspectData: prepared => { assert.equal(prepared.title, source); assert.equal(prepared.body, source); assert.equal(prepared.diff, sourceDiff); },
    inspectBody: body => assert.ok(body.startsWith(source + "\n\n")),
  });
  assert.equal((await reviewAndPost({ env, root: "/unused", ...f })).failure_class, "completed");
  await withRoot(async root => {
    let stdin = "";
    await runModel({ executable: env.CLAUDE_EXECUTABLE, data: { ...data, title: source, body: source, diff: sourceDiff }, oauthToken: "fake-oauth", root,
      spawnImpl: (...args) => { const child = childProcess([init, success])(...args); child.stdin.on("data", chunk => { stdin += chunk; }); return child; },
    });
    assert.deepEqual(JSON.parse(stdin), { ...data, title: source, body: source, diff: sourceDiff });
  });
});

test("ambiguous URL source spans and encoding beyond eight layers stop before model dispatch", async () => {
  let layered = "https://example.invalid/runbook?token=" + encodedSecret;
  for (let round = 0; round < 9; round++) layered = layered.replaceAll("%", "%25").replace(/^h/, "%68");
  for (const source of [`"https://example.invalid/runbook?token=${encodedSecret}`, layered]) {
    const f = fixture({ metadata: { title: source } });
    await assert.rejects(reviewAndPost({ env, root: "/unused", ...f }), error => isClass("reviewer-errored")(error) && !error.message.includes(encodedSecret));
    assert.equal(f.counts().models, 0);
    assert.equal(f.counts().posts, 0);
    assert.equal(f.counts().revocations, 1);
  }
});

test("plain credential URLs on separate source lines are removed from PR fields, CLI stdin, and returned review", async () => {
  const urls = [
    "https://GENERATED_TITLE_USER:GENERATED_TITLE_PASS@example.invalid/title?token=GENERATED_TITLE_QUERY#GENERATED_TITLE_FRAGMENT",
    "https://example.invalid/body#GENERATED_BODY_FRAGMENT",
    "https://example.invalid/deleted?token=GENERATED_DIFF_QUERY&source=GENERATED_DIFF_SOURCE",
  ];
  const f = fixture({
    metadata: { title: urls[0], body: urls[1] }, diff: diff.replace("-old", `-${urls[2]}`), text: urls.join("\n"),
    inspectData: prepared => {
      assert.ok(!JSON.stringify(prepared).includes("GENERATED_"));
      for (const [field, pathname] of [["title", "/title"], ["body", "/body"], ["diff", "/deleted"]]) assert.ok(prepared[field].includes(pathname), field);
    },
    inspectBody: body => {
      assert.ok(!body.includes("GENERATED_"));
      for (const pathname of ["/title", "/body", "/deleted"]) assert.ok(body.includes(pathname));
    },
  });
  assert.equal((await reviewAndPost({ env, root: "/unused", ...f })).failure_class, "completed");
  assert.equal(f.counts().models, 1);
  assert.equal(f.counts().posts, 1);
  await withRoot(async root => {
    let stdin = "";
    const source = urls.join("\n");
    const sourceDiff = diff.replace("@@ -1 +1 @@", "@@ -1,3 +1 @@").replace("-old", urls.map(url => `-${url}`).join("\n"));
    await runModel({ executable: env.CLAUDE_EXECUTABLE, data: { ...data, title: source, body: source, diff: sourceDiff }, oauthToken: "fake-oauth", root,
      spawnImpl: (...args) => {
        const child = childProcess([init, success])(...args);
        child.stdin.on("data", chunk => { stdin += chunk; });
        return child;
      },
    });
    for (const field of ["title", "body", "diff"]) {
      const input = JSON.parse(stdin)[field];
      assert.ok(!input.includes("GENERATED_"), field);
      for (const pathname of ["/title", "/body", "/deleted"]) assert.ok(input.includes(pathname), field);
    }
  });
});

test("encoded values removed from a plain URL query do not block review or reach CLI stdin or a comment", async () => {
  const secret = `GENERATED_QUERY_SECRET_${randomUUID()}`;
  const url = `https://example.invalid/path?token=${secret}\\u0068%68&#104;&#x68&amp;#${secret}`;
  const assertRedacted = text => {
    assert.ok(!text.includes(secret));
    assert.ok(text.includes("https://example.invalid/path"));
    for (const encoded of ["\\u0068", "%68", "&#104;", "&#x68", "&amp;"]) assert.ok(!text.includes(encoded));
  };
  const f = fixture({
    metadata: { title: url, body: url }, diff: diff.replace("-old", `-${url}`), text: url,
    inspectData: prepared => {
      for (const field of ["title", "body", "diff"]) assertRedacted(prepared[field]);
    },
    inspectBody: assertRedacted,
  });
  assert.equal((await reviewAndPost({ env, root: "/unused", ...f })).failure_class, "completed");
  assert.equal(f.counts().models, 1);
  assert.equal(f.counts().posts, 1);
  await withRoot(async root => {
    let stdin = "";
    const result = await runModel({ executable: env.CLAUDE_EXECUTABLE, data: { ...data, title: url, body: url, diff: diff.replace("-old", `-${url}`) }, oauthToken: "fake-oauth", root,
      spawnImpl: (...args) => {
        const child = childProcess([init, success])(...args);
        child.stdin.on("data", chunk => { stdin += chunk; });
        return child;
      },
    });
    assert.equal(result, success.result);
    const input = JSON.parse(stdin);
    for (const field of ["title", "body", "diff"]) assertRedacted(input[field]);
  });
});

test("multiple-at URL userinfo cannot enter prepared data, CLI stdin, or posted review", async () => {
  const pieces = ["USER", "PREFIX", "SECRET_SUFFIX"].map(label => `GENERATED_${label}_${randomUUID()}`);
  const url = `https://${pieces[0]}:${pieces[1]}@${pieces[2]}@example.invalid/path`;
  const assertRedacted = text => {
    for (const piece of pieces) assert.ok(!text.includes(piece));
    assert.ok(text.includes("https://example.invalid/path"));
  };
  const f = fixture({
    metadata: { title: url, body: url }, diff: diff.replace("-old", `-${url}`), text: url,
    inspectData: prepared => {
      for (const field of ["title", "body", "diff"]) assertRedacted(prepared[field]);
    },
    inspectBody: assertRedacted,
  });
  assert.equal((await reviewAndPost({ env, root: "/unused", ...f })).failure_class, "completed");
  assert.equal(f.counts().posts, 1);
  await withRoot(async root => {
    let stdin = "";
    await runModel({ executable: env.CLAUDE_EXECUTABLE, data: { ...data, title: url, body: url, diff: diff.replace("-old", `-${url}`) }, oauthToken: "fake-oauth", root,
      spawnImpl: (...args) => {
        const child = childProcess([init, success])(...args);
        child.stdin.on("data", chunk => { stdin += chunk; });
        return child;
      },
    });
    const input = JSON.parse(stdin);
    for (const field of ["title", "body", "diff"]) assertRedacted(input[field]);
  });
});

for (const field of ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"]) {
  test(field + " rejects zero and unsafe numeric IDs before requesting credentials", async () => {
    for (const value of ["0", "9007199254740992"]) {
      let fetched = false;
      await assert.rejects(reviewAndPost({ env: { ...env, [field]: value }, root: "/unused", fetchImpl: () => { fetched = true; throw new Error("No request allowed"); } }), isClass("startup-failure"));
      assert.equal(fetched, false);
    }
  });
}

test("failed producer writes a typed run-bound envelope with no comment ID", async t => {
  t.mock.method(console, "error", () => {});
  t.mock.method(console, "log", () => {});
  t.mock.method(globalThis, "fetch", () => { throw new Error("No request allowed"); });
  await withRoot(async root => {
    const output = path.join(root, "github-output");
    assert.equal(await reviewMain({ ...env, CLAUDE_CODE_OAUTH_TOKEN: "", RUNNER_TEMP: root, GITHUB_OUTPUT: output }), 1);
    const fields = Object.fromEntries(readFileSync(output, "utf8").trim().split("\n").map(line => line.split("=")));
    const result = JSON.parse(readFileSync(fields.result_file, "utf8"));
    assert.deepEqual(result, { version: 1, repository: "example/repo", pr_number: 7, workflow_run: 81, attempt: 2, failure_class: "startup-failure", reviewed_head_sha: head, producer_sha: producer });
    assert.deepEqual(JSON.parse(readFileSync(fields.execution_file, "utf8")), result);
    assert.equal(fields.comment_id, "");
  });
});
