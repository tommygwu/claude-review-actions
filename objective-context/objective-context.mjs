import { randomUUID } from "node:crypto";
import { appendFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

function openingFence(line) {
  const match = line.match(/^( {0,3})((?:[-*+]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)$/);
  if (!match || (match[3][0] === "`" && match[4].includes("`"))) return null;
  return { marker: match[3], indentation: match[2] ? match[1].length + match[2].length : 0 };
}

function codeSpanEnd(text, start, delimiter) {
  const runs = /`+/g;
  runs.lastIndex = start;
  let closing;
  while ((closing = runs.exec(text))) {
    if (closing[0] === delimiter) return closing.index + closing[0].length;
  }
  return -1;
}

function visibleLines(markdown) {
  let fence = null;
  let comment = false;
  let consumedThrough = -1;
  return markdown.replace(/\r\n?/g, "\n").split("\n").map((line, index, lines) => {
    if (index <= consumedThrough) return "";
    if (fence) {
      const closing = line.match(/^( *)(`{3,}|~{3,})\s*$/);
      if (closing && closing[1].length >= fence.indentation && closing[1].length <= fence.indentation + 3
          && closing[2][0] === fence.marker[0] && closing[2].length >= fence.marker.length) fence = null;
      return "";
    }
    const opening = !comment && openingFence(line);
    if (opening) {
      fence = opening;
      return "";
    }
    let visible = "";
    let cursor = 0;
    while (cursor < line.length) {
      const next = comment ? /-->/g : /<!--|`+/g;
      next.lastIndex = cursor;
      const marker = next.exec(line);
      if (!marker) {
        if (!comment) visible += line.slice(cursor);
        break;
      }
      if (!comment) visible += line.slice(cursor, marker.index);
      cursor = marker.index + marker[0].length;
      if (marker[0][0] === "`") {
        let end = codeSpanEnd(line, cursor, marker[0]);
        if (end < 0) {
          let continued = line;
          for (let next = Math.max(index, consumedThrough) + 1;
            next < lines.length && /^(?: {2,}|\t)\S/.test(lines[next]) && !heading(lines[next]); next += 1) {
            continued += `\n${lines[next]}`;
            end = codeSpanEnd(continued, cursor, marker[0]);
            if (end >= 0) {
              line = continued;
              consumedThrough = next;
              break;
            }
          }
        }
        cursor = end < 0 ? cursor : end;
        visible += line.slice(marker.index, cursor);
      } else {
        comment = !comment;
      }
    }
    fence = openingFence(visible);
    return fence ? "" : visible;
  });
}

function heading(line) {
  const match = line.match(/^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$/);
  return match ? { level: match[1].length, title: (match[2] ?? "").replace(/[ \t]+#+[ \t]*$/, "") } : null;
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
    const match = line.match(/^(?:[-*+]|\d+[.)])[ \t]+([\s\S]+?)\s*$/);
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
