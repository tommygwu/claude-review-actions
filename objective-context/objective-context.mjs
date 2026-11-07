import { randomUUID } from "node:crypto";
import { appendFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

function visibleLines(markdown) {
  let fence = null;
  let comment = false;
  return markdown.replace(/\r\n?/g, "\n").split("\n").map((line) => {
    if (fence) {
      const closing = line.match(/^ {0,3}(`{3,}|~{3,})\s*$/);
      if (closing && closing[1][0] === fence[0] && closing[1].length >= fence.length) fence = null;
      return "";
    }
    let visible = "";
    let cursor = 0;
    while (cursor < line.length) {
      const marker = line.indexOf(comment ? "-->" : "<!--", cursor);
      if (marker < 0) {
        if (!comment) visible += line.slice(cursor);
        break;
      }
      if (!comment) visible += line.slice(cursor, marker);
      cursor = marker + (comment ? 3 : 4);
      comment = !comment;
    }
    const opening = visible.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (opening && !(opening[1][0] === "`" && opening[2].includes("`"))) {
      fence = opening[1];
      return "";
    }
    return visible;
  });
}

function heading(line) {
  const match = line.match(/^ {0,3}(#{1,6})[ \t]+(.+?)\s*$/);
  return match ? { level: match[1].length, title: match[2].replace(/[ \t]+#+[ \t]*$/, "") } : null;
}

function sections(lines, level, title) {
  const found = [];
  for (let index = 0; index < lines.length; index += 1) {
    const current = heading(lines[index]);
    if (current?.level !== level || current.title !== title) continue;
    let end = index + 1;
    while (end < lines.length && !(heading(lines[end])?.level <= level)) end += 1;
    found.push(lines.slice(index + 1, end));
  }
  return found;
}

function bullets(lines) {
  const result = [];
  let active = false;
  for (const line of lines) {
    if (heading(line)) break;
    const match = line.match(/^(?:[-*+]|\d+[.)])[ \t]+(.+?)\s*$/);
    if (match) {
      result.push(match[1]);
      active = true;
    } else if (active && /^(?: {2,}|\t)\S/.test(line)) {
      result[result.length - 1] += `\n${line}`;
    } else if (line.trim()) {
      active = false;
    }
  }
  return result;
}

function bulletSection(intent, title) {
  const matches = sections(intent, 3, title);
  if (!matches.length) return { present: false, valid: false, bullets: [] };
  if (matches.length > 1) return { present: true, valid: false, bullets: [], error: "duplicate-heading" };
  const values = bullets(matches[0]);
  const isNone = (value) => /^(?:none|n\/a|not applicable)\.?$/i.test(value);
  return {
    present: true,
    valid: values.length > 0,
    bullets: values.filter((value) => !isNone(value)),
    ...(values.length ? {} : { error: "empty-section" }),
  };
}

function fallbackCandidates(lines) {
  const summary = sections(lines, 1, "Summary");
  let owner = null;
  const legacyIntent = lines.map((line) => {
    const current = heading(line);
    if (current?.level === 1) owner = current.title;
    return owner === null || owner === "Intent" ? line : "";
  });
  const why = sections(legacyIntent, 3, "Why").concat(sections(legacyIntent, 3, "Why?"));
  const candidates = summary.length ? summary : why;
  if (candidates.length !== 1) return [];
  const result = [];
  for (const line of candidates[0]) {
    if (heading(line)) break;
    const text = line.trim().replace(/^(?:[-*+]|\d+[.)])[ \t]+/, "");
    if (text && !/^(?:none|n\/a|not applicable)\.?$/i.test(text)) result.push(text);
  }
  return result;
}

export function extractObjectiveContext(markdown) {
  if (typeof markdown !== "string") throw new TypeError("PR body must be a string");
  const lines = visibleLines(markdown);
  const intents = sections(lines, 1, "Intent");
  const intent = intents.flat();
  const promised = bulletSection(intent, "Acceptance Criteria");
  const nonGoals = bulletSection(intent, "Non-goals");
  const duplicateIntent = intents.length > 1;
  const errors = [
    ...(duplicateIntent ? ["Intent:duplicate-heading"] : []),
    ...(promised.error ? [`Acceptance Criteria:${promised.error}`] : []),
    ...(nonGoals.error ? [`Non-goals:${nonGoals.error}`] : []),
  ];
  const fallback = promised.present || duplicateIntent ? [] : fallbackCandidates(lines);
  return {
    source: promised.present || duplicateIntent ? "structured" : fallback.length ? "fallback" : "none",
    hasStructuredSections: promised.present || nonGoals.present,
    promisedBehaviors: duplicateIntent ? [] : promised.bullets,
    nonGoals: duplicateIntent ? [] : nonGoals.bullets,
    fallbackCandidates: fallback,
    validSections: {
      promisedBehaviors: promised.valid && !duplicateIntent,
      nonGoals: nonGoals.valid && !duplicateIntent,
    },
    errors,
  };
}

export function renderObjective(context) {
  const lines = [`Objective source: ${context.source}`];
  if (context.errors.length) lines.push(`Extraction errors: ${context.errors.join(", ")}. Do not treat malformed objectives as verified compliance.`);
  for (const [title, values] of [
    ["Acceptance Criteria", context.promisedBehaviors],
    ["Non-goals", context.nonGoals],
    ["Fallback candidates (inferred, confirm against the diff)", context.fallbackCandidates],
  ]) {
    if (values.length) lines.push(`### ${title}`, ...values.map((value) => `- ${value}`));
  }
  if (!context.promisedBehaviors.length && !context.fallbackCandidates.length) {
    lines.push("No checkable positive objective was extracted; this is not proof of compliance. Check any non-goals listed above.");
  }
  return lines.join("\n");
}

function main() {
  if (!process.env.GITHUB_OUTPUT) throw new Error("GITHUB_OUTPUT is required");
  const context = extractObjectiveContext(process.env.PR_BODY ?? "");
  const outputs = { context: JSON.stringify(context), objective: renderObjective(context), source: context.source };
  const records = Object.entries(outputs).map(([name, value]) => {
    let delimiter;
    do { delimiter = `objective_${randomUUID()}`; } while (value.split("\n").includes(delimiter));
    return `${name}<<${delimiter}\n${value}\n${delimiter}\n`;
  });
  appendFileSync(process.env.GITHUB_OUTPUT, records.join(""));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
