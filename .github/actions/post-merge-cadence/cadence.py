"""Derive post-merge test coverage from GitHub run and attempt records."""

import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


CADENCES = ("every-merge", "newest-merge", "interval")
CHECK_STATUSES = {"queued", "in_progress", "completed", "waiting", "requested", "pending"}
CHECK_CONCLUSIONS = {"action_required", "cancelled", "failure", "neutral", "skipped", "stale",
                     "startup_failure", "success", "timed_out"}
CHECK_SUITE_BATCH_SIZE = 20


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("GitHub timestamp has no timezone")
    return result


def query(url, **values):
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params.update(values)
    return urlunsplit(parts._replace(query=urlencode(params)))


def names(value):
    return list(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))


def check_state(record):
    state = record.get("status")
    conclusion = record.get("conclusion")
    if (not isinstance(state, str) or state.lower() not in CHECK_STATUSES or "conclusion" not in record
            or conclusion is not None and not isinstance(conclusion, str)
            or isinstance(conclusion, str) and conclusion.lower() not in CHECK_CONCLUSIONS
            or state.lower() == "completed" and not conclusion):
        raise ValueError("GitHub GraphQL returned an invalid check lifecycle")
    return state.lower(), conclusion.lower() if conclusion else None


class ApiError(ValueError):
    def __init__(self, code, method, resource):
        self.code = code
        super().__init__(f"GitHub API {method} {resource} failed (HTTP {code})")


class GitHub:
    def __init__(self, api_url, repository, token, graphql_url=None, server_url="https://github.com"):
        self.api_root = api_url.rstrip('/')
        self.root = f"{api_url.rstrip('/')}/repos/{repository}"
        self.repository_id = None
        self.graphql_url = graphql_url or f"{api_url.rstrip('/')}/graphql"
        if urlsplit(self.graphql_url).netloc != urlsplit(api_url).netloc:
            raise ValueError("GitHub GraphQL endpoint left the API host")
        self.job_prefix = f"{server_url.rstrip('/')}/{repository}/actions/runs/"
        self.git_url = f"{server_url.rstrip('/')}/{repository}.git"
        self.server_url = server_url.rstrip('/')
        self.token = token
        self.git_directory = None
        self.fetched_tips = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.git_directory is not None:
            self.git_directory.cleanup()

    def request(self, method, resource, payload=None):
        if not self.token:
            raise ValueError("github-token is required to read or rerun workflow history")
        url = resource if resource.startswith("https://") else self.root + resource
        allowed = url in (self.root, self.graphql_url) or url.startswith(self.root + "/")
        if not allowed and url.startswith(self.api_root + "/repositories/"):
            if self.repository_id is None:
                repository = self.get("")
                if type(repository.get("id")) is not int or repository["id"] <= 0:
                    raise ValueError("GitHub returned an invalid repository identity")
                self.repository_id = repository["id"]
            allowed = url.startswith(f"{self.api_root}/repositories/{self.repository_id}/")
        if not allowed:
            raise ValueError("GitHub pagination left the repository API")
        request = Request(url, method=method,
                          data=json.dumps(payload or {}).encode() if method == "POST" else None,
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Accept": "application/vnd.github+json",
                                   "Content-Type": "application/json",
                                   "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
                return (json.loads(payload) if payload else {}, response.headers.get("Link", ""))
        except HTTPError as error:
            raise ApiError(error.code, method, urlsplit(url).path) from None
        except URLError:
            raise ValueError("GitHub API is unavailable") from None

    def get(self, resource):
        return self.request("GET", resource)[0]

    def pages(self, resource, field, total_field="total_count", cap=None):
        url = query(resource, per_page=100)
        seen = set()
        rows = []
        total = None
        while url:
            if url in seen:
                raise ValueError("GitHub pagination repeated a page")
            seen.add(url)
            data, links = self.request("GET", url)
            if not isinstance(data.get(field), list) or type(data.get(total_field)) is not int:
                raise ValueError("GitHub returned an incomplete history response")
            if total is None:
                total = data[total_field]
            rows.extend(data[field])
            if cap and len(rows) >= cap:
                break
            url = next((part.split(";", 1)[0].strip()[1:-1]
                        for part in links.split(",") if 'rel="next"' in part), None)
        if len(rows) < min(total, cap or total):
            raise ValueError("GitHub history pagination ended before all records arrived")
        return rows, total

    def workflows(self):
        return self.pages("/actions/workflows", "workflows")[0]

    def runs(self, workflow_id, branch):
        resource = query(f"/actions/workflows/{workflow_id}/runs", event="push", branch=branch)
        result = {}
        bound = None
        while True:
            window = query(resource, created=f"<{bound}") if bound else resource
            rows, total = self.pages(window, "workflow_runs", cap=1000)
            for row in rows:
                result[row["id"]] = row
            if total <= 1000:
                break
            oldest = min(row["created_at"] for row in rows)
            boundary, _ = self.pages(query(resource, created=oldest), "workflow_runs")
            for row in boundary:
                result[row["id"]] = row
            if bound and timestamp(oldest) >= timestamp(bound):
                raise ValueError("GitHub run history window made no progress")
            bound = oldest
        return sorted(result.values(), key=lambda row: (timestamp(row["created_at"]), row["id"]), reverse=True)

    def attempt(self, run_id, number):
        return self.get(f"/actions/runs/{run_id}/attempts/{number}")

    def jobs(self, run_id, number):
        return self.pages(f"/actions/runs/{run_id}/attempts/{number}/jobs", "jobs")[0]

    def run(self, run_id):
        return self.get(f"/actions/runs/{run_id}")

    def checks(self, runs):
        def selection(cursor):
            return ("{ ... on CheckSuite { id status conclusion workflowRun { databaseId runAttempt } "
                    f"checkRuns(first:100, after:{json.dumps(cursor)}, filterBy:{{checkType:ALL}}) {{ "
                    "nodes { name status conclusion startedAt detailsUrl } "
                    "pageInfo { hasNextPage endCursor } totalCount } } }")

        by_suite = {run["check_suite_node_id"]: run for run in runs}
        if len(by_suite) != len(runs) or any(not isinstance(key, str) or not key for key in by_suite):
            raise ValueError("GitHub returned invalid workflow check-suite identities")
        pending = {key: None for key in by_suite}
        cursors = {key: set() for key in by_suite}
        result = {run["id"]: {"jobs": {}, "snapshot": None, "total": None} for run in runs}
        while pending:
            batch = list(pending)[:CHECK_SUITE_BATCH_SIZE]
            first_pages = all(pending[key] is None for key in batch)
            if first_pages:
                query_text = "{ nodes(ids:" + json.dumps(batch) + ") " + selection(None) + " }"
            else:
                query_text = "{ " + " ".join(
                    f"s{index}: node(id:{json.dumps(key)}) " + selection(pending[key])
                    for index, key in enumerate(batch)) + " }"
            payload, _ = self.request("POST", self.graphql_url, {"query": query_text})
            if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
                raise ValueError("GitHub GraphQL returned incomplete check history")
            if first_pages:
                nodes = payload["data"].get("nodes")
                if not isinstance(nodes, list) or len(nodes) != len(batch):
                    raise ValueError("GitHub GraphQL returned incomplete check suites")
            else:
                nodes = [payload["data"].get(f"s{index}") for index in range(len(batch))]
            for index, key in enumerate(batch):
                node = nodes[index]
                run = by_suite[key]
                if not isinstance(node, dict) or node.get("id") != key:
                    raise ValueError("GitHub GraphQL returned a missing or mismatched check suite")
                workflow_run = node.get("workflowRun")
                if not isinstance(workflow_run, dict) or workflow_run.get("databaseId") != run["id"]:
                    raise ValueError("GitHub GraphQL check suite belongs to another workflow run")
                attempt = workflow_run.get("runAttempt")
                if type(attempt) is not int or attempt < 1:
                    raise ValueError("GitHub GraphQL returned an invalid attempt snapshot")
                snapshot = (attempt, *check_state(node))
                record = result[run["id"]]
                if record["snapshot"] is not None and record["snapshot"] != snapshot:
                    raise ValueError("GitHub check-suite attempt changed during pagination")
                record["snapshot"] = snapshot
                connection = node.get("checkRuns")
                if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
                    raise ValueError("GitHub GraphQL returned incomplete check jobs")
                total = connection.get("totalCount")
                if type(total) is not int or total < 0 or record["total"] not in (None, total):
                    raise ValueError("GitHub check-job count changed during pagination")
                record["total"] = total
                for check in connection["nodes"]:
                    required = {"name", "status", "conclusion", "startedAt", "detailsUrl"}
                    if (not isinstance(check, dict) or not required <= check.keys()
                            or not isinstance(check["name"], str)):
                        raise ValueError("GitHub GraphQL returned an invalid check job")
                    state, conclusion = check_state(check)
                    if check["startedAt"] is not None:
                        if not isinstance(check["startedAt"], str):
                            raise ValueError("GitHub GraphQL returned an invalid check start time")
                        timestamp(check["startedAt"])
                    details = check.get("detailsUrl")
                    prefix = f"{self.job_prefix}{run['id']}/job/"
                    if not isinstance(details, str) or not details.startswith(prefix):
                        raise ValueError("GitHub check job belongs to another workflow run")
                    job_id = details[len(prefix):]
                    if not job_id.isascii() or not job_id.isdecimal() or not isinstance(check.get("status"), str):
                        raise ValueError("GitHub GraphQL returned an invalid check-job identity")
                    record["jobs"][job_id] = {
                        "name": check["name"], "status": state, "conclusion": conclusion,
                        "started_at": check.get("startedAt"),
                    }
                page = connection.get("pageInfo")
                if not isinstance(page, dict) or type(page.get("hasNextPage")) is not bool:
                    raise ValueError("GitHub GraphQL returned incomplete check pagination")
                if page["hasNextPage"]:
                    cursor = page.get("endCursor")
                    if not isinstance(cursor, str) or not cursor or cursor in cursors[key]:
                        raise ValueError("GitHub check pagination made no progress")
                    cursors[key].add(cursor)
                    pending[key] = cursor
                else:
                    if len(record["jobs"]) != total:
                        raise ValueError("GitHub check pagination ended before all jobs arrived")
                    del pending[key]
        return result

    def git(self, arguments, stdin=""):
        environment = os.environ.copy()
        authorization = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        environment.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.{self.server_url}/.extraheader",
                           GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {authorization}",
                           GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
        try:
            result = subprocess.run(["git", "-C", self.git_directory.name, *arguments],
                                    input=stdin, text=True, capture_output=True, env=environment, timeout=120)
        except FileNotFoundError:
            raise ValueError("Git is required to prove GitHub commit ancestry") from None
        except subprocess.TimeoutExpired:
            raise ValueError(f"GitHub commit ancestry {arguments[0]} timed out") from None
        if result.returncode:
            raise ValueError(f"GitHub commit ancestry {arguments[0]} failed (exit {result.returncode}); verify contents read and repository access")
        return result.stdout

    def coverage(self, tips):
        tips = set(tips)
        if not tips:
            return set()
        if any(not isinstance(tip, str) or len(tip) != 40 or any(char not in "0123456789abcdef" for char in tip)
               for tip in tips):
            raise ValueError("GitHub returned an invalid tested commit SHA")
        if self.git_directory is None:
            self.git_directory = tempfile.TemporaryDirectory(prefix="post-merge-cadence-", dir=os.environ.get("RUNNER_TEMP"))
            self.git(["init", "--bare"])
        missing = tips - self.fetched_tips
        if missing:
            self.git(["fetch", "--filter=tree:0", "--no-tags", "--stdin", self.git_url],
                     "\n".join(sorted(missing)) + "\n")
            if self.git(["rev-parse", "--is-shallow-repository"]).strip() != "false":
                raise ValueError("GitHub commit ancestry is shallow and cannot prove coverage")
            if set(self.git(["cat-file", "--batch-all-objects", "--batch-check=%(objecttype)"]).splitlines()) - {"commit"}:
                raise ValueError("GitHub commit snapshot contains objects other than commits")
            self.fetched_tips.update(missing)
        commits = set(self.git(["rev-list", "--stdin"], "\n".join(sorted(tips)) + "\n").splitlines())
        if not tips <= commits:
            raise ValueError("GitHub commit ancestry omitted a tested tip")
        return commits

    def rerun(self, run):
        try:
            self.request("POST", f"/actions/runs/{run['id']}/rerun")
        except ApiError as error:
            if error.code not in (409, 422):
                raise
            current = self.get(f"/actions/runs/{run['id']}")
            if current["status"] != "completed" or current["run_attempt"] != run["run_attempt"]:
                return False
            raise
        return True


class History:
    def __init__(self, api, workflow_id, branch, excluded):
        self.api = api
        self.runs = api.runs(workflow_id, branch)
        self.excluded = {"post-merge-config", *excluded}
        self.checks = {}
        self.changed_attempts = set()
        self.tested_cache = {}
        self.tips = set()
        self.loaded_tips = set()
        self.tested_shas = set()
        self.ancestry = set()
        self.coverage_complete = False

    def active(self):
        return any(run["status"] != "completed" for run in self.runs)

    def load_checks(self, target):
        ordered = sorted(self.runs, key=lambda row: timestamp(row["run_started_at"]), reverse=True)
        start = next(index for index, row in enumerate(ordered) if row["id"] == target["id"])
        batch = [row for row in ordered[start:] if row["id"] not in self.checks
                 and (row["id"] == target["id"] or row["head_sha"] not in self.tested_shas
                      and row["head_sha"] not in self.ancestry)][:CHECK_SUITE_BATCH_SIZE]
        self.checks.update(self.api.checks(batch))
        for run in batch:
            attempt, state, conclusion = self.checks[run["id"]]["snapshot"]
            if attempt != int(run["run_attempt"]):
                current = self.api.run(run["id"])
                if (current["id"] != run["id"] or current["check_suite_node_id"] != run["check_suite_node_id"]
                        or current["head_sha"] != run["head_sha"] or int(current["run_attempt"]) < attempt):
                    raise ValueError("GitHub run changed identity during check discovery")
                run.update(current)
                self.changed_attempts.add(run["id"])
            elif attempt == 1:
                run.update(status=state, conclusion=conclusion)
            if run["id"] in self.changed_attempts:
                continue
            positive = any(self.test_job(job) for job in self.checks[run["id"]]["jobs"].values())
            if not positive or int(run["run_attempt"]) == 1:
                tested = run if positive and run["conclusion"] != "cancelled" else None
                self.tested_cache[run["id"]] = tested
                if tested:
                    self.tested_shas.add(run["head_sha"])

    def tested(self, run):
        if run["id"] not in self.checks:
            self.load_checks(run)
        if run["id"] not in self.tested_cache:
            tested = None
            for number in range(int(run["run_attempt"]), 0, -1):
                attempt = self.api.attempt(run["id"], number)
                if attempt["id"] != run["id"] or int(attempt["run_attempt"]) != number:
                    raise ValueError("GitHub returned a mismatched workflow attempt")
                if attempt["conclusion"] == "cancelled":
                    continue
                if any(self.test_job(job) for job in self.api.jobs(run["id"], number)):
                    tested = attempt
                    break
            self.tested_cache[run["id"]] = tested
        if self.tested_cache[run["id"]]:
            self.add_coverage(run["head_sha"])
        return self.tested_cache[run["id"]]

    def test_job(self, job):
        return (job["name"] not in self.excluded and job["conclusion"] != "skipped"
                and job.get("started_at") and job["status"] in ("in_progress", "completed"))

    def is_covered(self, sha):
        if sha in self.tested_shas or sha in self.ancestry:
            return True
        self.load_ancestry()
        if sha in self.ancestry:
            return True
        if not self.coverage_complete:
            for run in self.runs:
                if run["head_sha"] not in self.ancestry and self.tested(run):
                    self.load_ancestry()
            self.coverage_complete = True
        return sha in self.tested_shas or sha in self.ancestry

    def add_coverage(self, sha):
        self.tips.add(sha)
        self.tested_shas.add(sha)

    def load_ancestry(self):
        if self.tips != self.loaded_tips:
            self.ancestry.update(self.api.coverage(self.tips))
            self.loaded_tips = self.tips.copy()

    def latest_tested(self):
        while True:
            latest = None
            for run in sorted(self.runs, key=lambda row: timestamp(row["run_started_at"]), reverse=True):
                if latest and timestamp(run["run_started_at"]) < timestamp(latest["run_started_at"]):
                    return latest
                if run["id"] not in self.checks:
                    self.load_checks(run)
                    break
                tested = self.tested(run)
                if tested and (latest is None or timestamp(tested["run_started_at"]) > timestamp(latest["run_started_at"])):
                    latest = tested
            else:
                return latest

    def uncovered(self, current_id, current_created):
        self.latest_tested()
        arrivals = {}
        for run in self.runs:
            sha = run["head_sha"]
            created = timestamp(run["created_at"])
            if run["id"] == current_id or created > current_created:
                continue
            arrivals[sha] = min(arrivals.get(sha, created), created)
        return {sha: created for sha, created in arrivals.items() if not self.is_covered(sha)}


def execute(inputs, env, event, api, now, summary):
    operation = inputs.get("operation", "decide")
    if operation not in ("decide", "check"):
        raise ValueError("operation must be decide or check")
    if operation == "decide" and env["GITHUB_EVENT_NAME"] != "push":
        return True
    ci = inputs.get("post-merge-ci", "").strip().lower()
    if ci not in ("true", "false"):
        raise ValueError("POST_MERGE_CI must be true or false")
    requested_workflows = names(inputs.get("test-workflows", ""))
    workflows = requested_workflows or [env["GITHUB_WORKFLOW"]]
    if operation == "check" and ci == "false":
        for workflow in workflows:
            summary(f"{workflow}: no tests; POST_MERGE_CI=false.")
        return None
    cadence = inputs.get("cadence", "")
    if cadence not in CADENCES:
        raise ValueError("POST_MERGE_CADENCE must be " + ", ".join(CADENCES))
    interval = None
    if cadence == "interval":
        hours = inputs.get("interval-hours", "")
        if not hours.isascii() or not hours.isdecimal() or int(hours) <= 0:
            raise ValueError("POST_MERGE_INTERVAL_HOURS must be a positive whole number")
        interval = timedelta(hours=int(hours))
    if ci == "false":
        summary(f"{env['GITHUB_WORKFLOW']}: no tests; POST_MERGE_CI=false.")
        return False
    if cadence != "interval":
        if operation == "check":
            for workflow in workflows:
                summary(f"{workflow}: no rerun; cadence is {cadence}.")
            return None
        return True
    if operation == "decide" and int(env["GITHUB_RUN_ATTEMPT"]) > 1:
        return True
    if operation == "check" and not requested_workflows:
        raise ValueError("test-workflows must explicitly list the test workflows for interval check")
    branch = event["repository"]["default_branch"]
    excluded = names(inputs.get("non-test-jobs", ""))
    catalog = api.workflows()
    if operation == "decide":
        workflows = [env["GITHUB_WORKFLOW"]]
        summary(f"{workflows[0]}: commit {env['GITHUB_SHA']} is not yet tested; interval attempt 1 skips tests.")
    for workflow in workflows:
        matches = [row for row in catalog if row["name"] == workflow]
        if len(matches) != 1:
            raise ValueError(f"Test workflow must match exactly one workflow: {workflow}")
        history = History(api, matches[0]["id"], branch, excluded)
        if operation == "decide":
            current_id = int(env["GITHUB_RUN_ID"])
            current = next((row for row in history.runs if row["id"] == current_id), None)
            if current is None:
                raise ValueError("Current push run is absent from workflow history")
            arrivals = history.uncovered(current_id, timestamp(current["created_at"]))
            if arrivals:
                oldest, arrived = min(arrivals.items(), key=lambda item: item[1])
                if now - arrived > interval * 3:
                    raise ValueError(f"{workflow}: stopped-test check failed; earlier commit {oldest} has no test coverage since {arrived.isoformat()} (more than three intervals)")
            return False
        if not history.runs:
            summary(f"{workflow}: no rerun; no default-branch push runs.")
            continue
        newest = history.runs[0]
        if history.active():
            summary(f"{workflow}: no rerun; a push run is still in progress or queued.")
            continue
        latest = history.latest_tested()
        if history.active():
            summary(f"{workflow}: no rerun; a push run became active during history discovery.")
            continue
        if history.tested(newest):
            history.add_coverage(newest["head_sha"])
        covered = history.is_covered(newest["head_sha"])
        if history.active():
            summary(f"{workflow}: no rerun; a push run became active during coverage discovery.")
            continue
        if covered:
            summary(f"{workflow}: no rerun; newest commit already has test coverage.")
            continue
        failed = latest and latest["conclusion"] in ("failure", "timed_out", "startup_failure", "action_required")
        if latest and not failed and now - timestamp(latest["run_started_at"]) < interval:
            summary(f"{workflow}: no rerun; interval has not passed since the last tested attempt.")
            continue
        reason = "last tested attempt failed" if failed else "interval is due" if latest else "no tested attempt exists"
        if api.rerun(newest):
            summary(f"{workflow}: requested rerun of push run {newest['id']}; {reason}.")
        else:
            summary(f"{workflow}: deferred rerun; the run became active or its attempt changed; the next check will reevaluate.")
    return False if operation == "decide" else None


def main():
    inputs = {key: os.environ.get("INPUT_" + key.upper().replace("-", "_"), "")
              for key in ("operation", "post-merge-ci", "cadence", "interval-hours", "test-workflows", "non-test-jobs")}
    with Path(os.environ["GITHUB_EVENT_PATH"]).open() as stream:
        event = json.load(stream)
    def summary(message):
        print(message)
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
            stream.write(message + "\n")

    with GitHub(os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                os.environ["GITHUB_REPOSITORY"], os.environ.get("INPUT_GITHUB_TOKEN", ""),
                os.environ.get("GITHUB_GRAPHQL_URL"), os.environ.get("GITHUB_SERVER_URL", "https://github.com")) as api:
        result = execute(inputs, os.environ, event, api, datetime.now(timezone.utc), summary)
    if result is not None:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"run_ci={str(result).lower()}\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, OverflowError) as error:
        sys.exit(f"post-merge-cadence: {error}")
