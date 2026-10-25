"""Extract and validate the version 1 allowlisted Actions event context."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent / "run-context.schema.json"
PR_EVENTS = {"pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment"}
MAX_BYTES = 64 * 1024


def positive_id(value, field):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError("run-context-environment-invalid:" + field)
    try:
        result = int(value)
    except ValueError:
        raise ValueError("run-context-environment-invalid:" + field) from None
    if result < 1:
        raise ValueError("run-context-environment-invalid:" + field)
    return result


def mapping(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("run-context-object-invalid")
    return value


def repository(value):
    return mapping(value).get("full_name")


def revision(value):
    value = mapping(value)
    return {"sha": value.get("sha"), "repository": repository(value.get("repo"))}


def pr_identity(value, fallback_repository=None):
    value = mapping(value)
    return {"number": value.get("number"), "repository": repository(mapping(value.get("base")).get("repo")) or fallback_repository,
            "head": revision(value.get("head")), "base": revision(value.get("base")),
            "merge_commit_sha": value.get("merge_commit_sha")}


def extract(event, env, observed_at):
    if not isinstance(event, dict):
        raise ValueError("run-context-event-invalid")
    name = env["GITHUB_EVENT_NAME"]
    pr = pr_identity(event["pull_request"]) if name in PR_EVENTS and event.get("pull_request") is not None else None
    if name == "issue_comment":
        issue = mapping(event.get("issue"))
        marker = issue.get("pull_request")
        if marker is not None:
            mapping(marker)
            pr = pr_identity(issue, env["GITHUB_REPOSITORY"])
    parent = event.get("workflow_run") if name == "workflow_run" else None
    if parent is not None:
        if not isinstance(parent, dict) or not isinstance(parent.get("pull_requests", []), list):
            raise ValueError("run-context-parent-invalid")
        parent = {"run_id": parent.get("id"), "run_attempt": parent.get("run_attempt"),
                  "workflow_id": parent.get("workflow_id"), "head_sha": parent.get("head_sha"),
                  "pull_requests": [pr_identity(value) for value in parent.get("pull_requests", [])]}
    result = {"schema": "ci-run-context", "version": 1, "repository": env["GITHUB_REPOSITORY"],
              "run_id": positive_id(env["GITHUB_RUN_ID"], "GITHUB_RUN_ID"),
              "capture_attempt": positive_id(env["GITHUB_RUN_ATTEMPT"], "GITHUB_RUN_ATTEMPT"),
              "event": name, "action": event.get("action"), "observed_at": observed_at,
              "workflow_ref": env["GITHUB_WORKFLOW_REF"], "workflow_sha": env["GITHUB_WORKFLOW_SHA"],
              "actions_sha": env["GITHUB_SHA"], "actions_ref": env["GITHUB_REF"],
              "pull_request": pr, "parent": parent}
    validate(result)
    return result


def check(value, schema, root, path="context"):
    def invalid(reason):
        raise ValueError(f"run-context-schema-invalid:{path}:{reason}")

    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return check(value, target, root, path)
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        invalid("const")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    table = {"object": dict, "array": list, "string": str, "integer": int, "null": type(None)}
    if types and not any(isinstance(value, table[kind]) and not (kind == "integer" and isinstance(value, bool)) for kind in types):
        invalid("type")
    if isinstance(value, dict):
        if set(schema.get("required", [])) - value.keys():
            invalid("required-member")
        for key, item in value.items():
            rule = schema.get("properties", {}).get(key, schema.get("additionalProperties", {}))
            if rule is False:
                invalid("extra-member")
            check(item, rule, root, path + "." + key)
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", len(value)):
            invalid("array-limit")
        for index, item in enumerate(value):
            check(item, schema.get("items", {}), root, f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", len(value)) or "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            invalid("string-shape")
    if type(value) is int and value < schema.get("minimum", value):
        invalid("minimum")


def validate(value):
    if len(json.dumps(value).encode()) > MAX_BYTES:
        raise ValueError("run-context-too-large")
    schema = json.loads(SCHEMA.read_text())
    check(value, schema, schema)
    try:
        observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("run-context-time-invalid") from None
    if observed.tzinfo is None:
        raise ValueError("run-context-time-invalid")

