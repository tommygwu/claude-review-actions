import copy
import io
import hashlib
import re
import subprocess
import json
import os
from pathlib import Path
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit

import cadence


ACTION = Path(__file__).resolve().parent
RECORDED = json.loads((ACTION / "fixtures/recorded-history.json").read_text())
ROOT = "https://api.github.com/repos/owner/repo"
NOW = datetime(2000, 1, 2, 12, tzinfo=timezone.utc)
TOKEN = "TEST-ONLY-SECRET"
SUBPROCESS_RUN = subprocess.run
BLOB = b"fixture\n"
BLOB_ID = hashlib.sha1(f"blob {len(BLOB)}\0".encode() + BLOB).hexdigest()
TREE = b"100644 fixture\0" + bytes.fromhex(BLOB_ID)
TREE_ID = hashlib.sha1(f"tree {len(TREE)}\0".encode() + TREE).hexdigest()


def clock(hours):
    return (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def run(identifier, sha, age, *, tested=False, conclusion="success", attempt=1,
        started=None, status="completed", workflow=1):
    row = copy.deepcopy(RECORDED["runs"]["workflow_runs"][0])
    row.update(id=identifier, head_sha=sha, created_at=clock(age), run_started_at=clock(age if started is None else started),
               status=status, conclusion=conclusion, run_attempt=attempt, workflow_id=workflow,
               check_suite_node_id=f"CS_scenario_{identifier}", event="push", head_branch="main")
    jobs = copy.deepcopy(RECORDED["completed_jobs"]["jobs"][:1])
    if tested:
        jobs.append(copy.deepcopy(RECORDED["completed_jobs"]["jobs"][1]))
        jobs[-1].update(status="in_progress" if status == "in_progress" else "completed",
                        conclusion=None if status == "in_progress" else conclusion)
    return row, jobs


class Response:
    def __init__(self, payload, links=""):
        self.payload = json.dumps(payload).encode()
        self.headers = {"Link": links}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class Transport:
    def __init__(self):
        self.routes = {}
        self.calls = []
        self.graphql = None

    def key(self, method, resource):
        parts = urlsplit(resource)
        return method, parts.path, tuple(sorted((key, tuple(value)) for key, value in parse_qs(parts.query).items()))

    def add(self, resource, payload, *, method="GET", links=""):
        self.routes[self.key(method, ROOT + resource)] = (payload, links)

    def __call__(self, request, timeout):
        self.calls.append(request)
        if request.full_url == "https://api.github.com/graphql":
            return Response(self.graphql(json.loads(request.data)["query"]))
        key = self.key(request.method, request.full_url)
        if key not in self.routes:
            raise AssertionError(f"Unexpected GitHub request: {request.method} {request.full_url}")
        payload, links = self.routes[key]
        if callable(payload):
            payload = payload(request)
        if isinstance(payload, Exception):
            if isinstance(payload, HTTPError):
                payload.close()
            raise payload
        return Response(payload, links)

    def posts(self):
        return [request.full_url for request in self.calls
                if request.method == "POST" and request.full_url != "https://api.github.com/graphql"]


class GitGraph:
    """A commit-only test repository with declared, independently constructed parents."""

    def __init__(self, labels, parents):
        self.objects = {}
        self.identities = {}
        self.parents = parents
        for label in labels:
            self.commit(label)
        self.directory = None
        self.calls = []
        self.failure = None
        self.artifacts = []

    def commit(self, label):
        if label not in self.identities:
            parents = [self.commit(parent) for parent in self.parents.get(label, [])]
            content = (f"tree {TREE_ID}\n"
                       + "".join(f"parent {parent}\n" for parent in parents)
                       + "author Test <test@example.invalid> 1700000000 +0000\n"
                       + "committer Test <test@example.invalid> 1700000000 +0000\n\n" + label + "\n")
            raw = content.encode()
            oid = hashlib.sha1(f"commit {len(raw)}\0".encode() + raw).hexdigest()
            self.objects[oid] = content
            self.identities[label] = oid
        return self.identities[label]

    def materialize(self, root):
        if self.directory is not None:
            return
        self.directory = Path(root) / "source.git"
        self.directory.mkdir()
        def git(*args, stdin=""):
            return SUBPROCESS_RUN(["git", "-C", str(self.directory), *args], input=stdin, text=True,
                                  capture_output=True, check=True).stdout
        git("init", "--bare")
        git("config", "uploadpack.allowFilter", "true")
        objects = [("blob", BLOB_ID, BLOB), ("tree", TREE_ID, TREE)]
        objects.extend(("commit", oid, content.encode()) for oid, content in self.objects.items())
        for kind, oid, raw in objects:
            target = self.directory / "objects" / oid[:2] / oid[2:]
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(zlib.compress(f"{kind} {len(raw)}\0".encode() + raw))
        git("update-ref", "--stdin", stdin="".join(
            f"update refs/heads/root-{index} {oid}\n" for index, oid in enumerate(self.objects)))

    def transport(self, arguments, **kwargs):
        self.calls.append((list(arguments), kwargs.copy()))
        if self.failure:
            result = self.failure(arguments, kwargs)
            if result is not None:
                return result
        if "fetch" in arguments:
            self.materialize(self.runner_temp)
            arguments = [self.directory.as_uri() if value == "https://github.com/owner/repo.git" else value
                         for value in arguments]
        result = SUBPROCESS_RUN(arguments, **kwargs)
        for name in ("config", "FETCH_HEAD"):
            artifact = Path(arguments[2]) / name
            if artifact.exists():
                self.artifacts.append(artifact.read_text())
        return result


class Scenario:
    def __init__(self, records=(), *, workflows=None, ancestry=None):
        records = copy.deepcopy(records)
        self.http = Transport()
        self.http.graphql = self.graphql
        self.graph = GitGraph({row["head_sha"] for row, _ in records}, ancestry or {})
        self.snapshots = {}
        self.attempt_jobs = {}
        self.suite_overrides = {}
        self.rows = {}
        workflows = workflows or [(1, "Tests")]
        self.http.add("/actions/workflows?per_page=100", {
            "total_count": len(workflows), "workflows": [{"id": identifier, "name": name} for identifier, name in workflows]})
        for row, jobs in records:
            row.setdefault("event", "push")
            row.setdefault("head_branch", "main")
            row["head_sha"] = self.graph.identities[row["head_sha"]]
            self.rows[row["id"]] = row
            self.snapshots[row["check_suite_node_id"]] = row.copy()
            self.attempt(row, row["run_attempt"], jobs)
            self.http.add(f"/actions/runs/{row['id']}", row)
            self.http.add(f"/actions/runs/{row['id']}/rerun", {}, method="POST")
        for identifier, _ in workflows:
            self.history(identifier, [row for row, _ in records if row["workflow_id"] == identifier])

    def history(self, workflow, rows):
        self.http.add(f"/actions/workflows/{workflow}/runs?event=push&branch=main&per_page=1",
                      {"total_count": len(rows), "workflow_runs": rows[:1]})
        resource = f"/actions/workflows/{workflow}/runs?event=push&branch=main&per_page=100"
        def pages(url, items, total=None):
            for offset in range(0, max(len(items), 1), 100):
                page = offset // 100 + 1
                endpoint = url + (f"&page={page}" if page > 1 else "")
                self.http.add(endpoint, {"total_count": len(items) if total is None else total,
                                        "workflow_runs": items[offset:offset + 100]},
                              links=f'<{ROOT}{url}&page={page + 1}>; rel="next"' if offset + 100 < len(items) else "")
        if len(rows) <= 1000:
            pages(resource, rows)
        else:
            first = rows[:1000]
            pages(resource, first, len(rows))
            boundary = first[-1]["created_at"]
            pages(resource + "&" + urlencode({"created": boundary}),
                  [row for row in rows if row["created_at"] == boundary])
            pages(resource + "&" + urlencode({"created": "<" + boundary}),
                  [row for row in rows if row["created_at"] < boundary])

    def attempt(self, row, number, jobs, **updates):
        jobs = copy.deepcopy(jobs)
        for index, job in enumerate(jobs):
            job.update(id=row["id"] * 100000 + number * 1000 + index,
                       run_id=row["id"], run_attempt=number)
        attempt = copy.deepcopy(RECORDED["attempt"])
        attempt.update(row, run_attempt=number)
        attempt.update(updates)
        self.http.add(f"/actions/runs/{row['id']}/attempts/{number}", attempt)
        self.http.add(f"/actions/runs/{row['id']}/attempts/{number}/jobs?per_page=100",
                      {"total_count": len(jobs), "jobs": jobs})
        self.attempt_jobs[(row["id"], number)] = copy.deepcopy(jobs)

    def suite(self, key):
        row = self.snapshots[key]
        jobs = []
        for (identifier, number), attempt_jobs in self.attempt_jobs.items():
            if identifier == row["id"]:
                for job in attempt_jobs:
                    jobs.append({"name": job["name"], "status": job["status"].upper(),
                                 "conclusion": job["conclusion"].upper() if job["conclusion"] else None,
                                 "startedAt": job.get("started_at"),
                                 "detailsUrl": f"https://github.com/owner/repo/actions/runs/{identifier}/job/{job['id']}"})
        return {"id": key, "status": row["status"].upper(),
                "conclusion": row["conclusion"].upper() if row["conclusion"] else None,
                "workflowRun": {"databaseId": row["id"], "runAttempt": row["run_attempt"]},
                "checkRuns": {"nodes": jobs, "totalCount": len(jobs)}}

    def graphql(self, query):
        if not re.search(r"filterBy\s*:\s*\{\s*checkType\s*:\s*ALL\s*\}", query):
            raise AssertionError("The request must select ALL attempt check runs")
        first = re.search(r'nodes\(ids:(\[[^\n]+?\])\)', query)
        if first:
            if not re.search(r'after:null', query):
                raise AssertionError("Shared nodes query must request first pages")
            selections = [(str(index), json.dumps(key), "null")
                          for index, key in enumerate(json.loads(first[1]))]
        else:
            selections = re.findall(r'(s[0-9]+): node\(id:("[^"\n]+")\).*?after:("[^"\n]+")', query)
            if not selections:
                raise AssertionError("Expected nodes first pages or independent continuation cursors")
        data = {}
        for alias, encoded_key, encoded_cursor in selections:
            key = json.loads(encoded_key)
            cursor = json.loads(encoded_cursor)
            node = copy.deepcopy(self.suite_overrides.get(key, self.suite(key)))
            if callable(node):
                data[alias] = node(cursor)
                continue
            if node is None:
                data[alias] = None
                continue
            connection = node["checkRuns"]
            offset = int(cursor) if cursor is not None else 0
            jobs = connection["nodes"]
            connection["nodes"] = jobs[offset:offset + 100]
            connection["pageInfo"] = {"hasNextPage": offset + 100 < len(jobs),
                                      "endCursor": str(offset + 100) if offset + 100 < len(jobs) else None}
            data[alias] = node
        return {"data": {"nodes": list(data.values())} if first else data}

    def execute(self, *, operation="check", env=None, **inputs):
        settings = {"operation": operation, "post-merge-ci": "true", "cadence": "interval", "interval-hours": "4",
                    "test-workflows": "Tests"}
        settings.update(inputs)
        settings = {key: value for key, value in settings.items() if value is not None}
        environment = {"GITHUB_EVENT_NAME": "push", "GITHUB_WORKFLOW": "Tests", "GITHUB_RUN_ATTEMPT": "1",
                       "GITHUB_RUN_ID": "3", "GITHUB_SHA": "c"}
        environment.update(env or {})
        self.summaries = []
        with tempfile.TemporaryDirectory() as directory:
            self.graph.runner_temp = directory
            with patch.dict(os.environ, {"RUNNER_TEMP": directory}), patch.object(cadence, "urlopen", self.http), \
                    patch.object(cadence.subprocess, "run", self.graph.transport):
                with cadence.GitHub("https://api.github.com", "owner/repo", TOKEN) as api:
                    try:
                        return cadence.execute(settings, environment, {"repository": {"default_branch": "main"}},
                                               api, NOW, self.summaries.append)
                    finally:
                        self.materialization = api.git_directory.name if api.git_directory else None



class Decisions(unittest.TestCase):
    def test_push_mode_attempt_and_ci_decisions(self):
        cases = (
            ("every-merge", 1, True), ("every-merge", 2, True), ("every-merge", 7, True),
            ("newest-merge", 1, True), ("newest-merge", 2, True), ("newest-merge", 7, True),
            ("interval", 1, False), ("interval", 2, True), ("interval", 7, True),
        )
        for mode, attempt, enabled_result in cases:
            for enabled, expected in (("TrUe", enabled_result), ("FaLsE", False)):
                with self.subTest(mode=mode, attempt=attempt, enabled=enabled):
                    scenario = Scenario([run(3, "c", 0)])
                    result = scenario.execute(operation="decide", cadence=mode, **{"post-merge-ci": enabled},
                                              env={"GITHUB_RUN_ATTEMPT": str(attempt)})
                    self.assertIs(expected, result)
                    if enabled == "FaLsE":
                        self.assertEqual([], scenario.http.calls)
                    if (mode, attempt, enabled) == ("interval", 1, "TrUe"):
                        self.assertTrue(scenario.summaries)

    def test_non_push_decisions_ignore_false_unset_empty_and_invalid_post_merge_settings(self):
        for event in ("pull_request", "workflow_dispatch", "schedule"):
            for enabled in ("TrUe", "FaLsE", "", None, "invalid"):
                for mode, hours in ((None, None), ("", ""), ("unknown", "bad"), ("interval", "0"),
                                    ("every-merge", "bad"), ("newest-merge", None), ("interval", "4")):
                    with self.subTest(event=event, enabled=enabled, cadence=mode, hours=hours):
                        scenario = Scenario()
                        self.assertIs(True, scenario.execute(
                            operation="decide", cadence=mode,
                            **{"post-merge-ci": enabled, "interval-hours": hours},
                            env={"GITHUB_EVENT_NAME": event}))
                        self.assertEqual([], scenario.http.calls)
                        self.assertEqual([], scenario.graph.calls)

    def test_invalid_settings_fail_and_unknown_cadence_lists_allowed_values(self):
        for operation, enabled in (("decide", "true"), ("decide", "FALSE"), ("check", "true")):
            for value in (None, "", "sometimes", "INTERVAL"):
                with self.subTest(operation=operation, enabled=enabled, cadence=value):
                    scenario = Scenario()
                    with self.assertRaises(ValueError) as failure:
                        scenario.execute(operation=operation, cadence=value, **{"post-merge-ci": enabled})
                    for allowed in ("every-merge", "newest-merge", "interval"):
                        self.assertIn(allowed, str(failure.exception))
                    self.assertEqual([], scenario.http.calls)
            for hours in (None, "", "0", "-1", "1.5", " 2", "２"):
                with self.subTest(operation=operation, enabled=enabled, hours=hours):
                    scenario = Scenario()
                    with self.assertRaises(ValueError):
                        scenario.execute(operation=operation, **{"post-merge-ci": enabled, "interval-hours": hours})
                    self.assertEqual([], scenario.http.calls)
        for field, values in {"post-merge-ci": (None, "", "yes", "0"),
                              "operation": ("", "rerun")}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    Scenario().execute(**{field: value})

    def test_disabled_and_non_interval_checks_never_read_history(self):
        for mode in ("every-merge", "newest-merge"):
            for workflows, summaries in (("Tests\nOther", 2), (None, 1)):
                with self.subTest(mode=mode, workflows=workflows):
                    scenario = Scenario()
                    self.assertIsNone(scenario.execute(cadence=mode, env={"GITHUB_WORKFLOW": "Hourly check"},
                                                      **{"interval-hours": "invalid", "test-workflows": workflows}))
                    self.assertEqual([], scenario.http.calls)
                    self.assertEqual(summaries, len(scenario.summaries))
        scenario = Scenario()
        self.assertIsNone(scenario.execute(cadence="bad", env={"GITHUB_WORKFLOW": "Hourly check"},
                                          **{"post-merge-ci": "FALSE", "interval-hours": "bad", "test-workflows": None}))
        self.assertEqual([], scenario.http.calls)

    def test_interval_check_rejects_omitted_empty_or_whitespace_workflow_targets_before_history(self):
        for workflows in (None, "", " \n\t"):
            with self.subTest(workflows=workflows):
                scenario = Scenario(workflows=[(2, "Hourly check")])
                with self.assertRaises(ValueError):
                    scenario.execute(env={"GITHUB_WORKFLOW": "Hourly check"}, **{"test-workflows": workflows})
                self.assertEqual([], scenario.http.calls)

    def test_interval_decide_checks_current_workflow_instead_of_supplied_targets(self):
        scenario = Scenario([run(3, "c", 0)])
        self.assertFalse(scenario.execute(operation="decide", **{"test-workflows": "Other"}))


class Checker(unittest.TestCase):
    def test_unavailable_candidate_probe_uses_full_history_and_keeps_due_rerun(self):
        for failure in (URLError("unavailable"), HTTPError(ROOT, 503, "unavailable", {}, None),
                        OSError("connection reset"), TimeoutError("read timed out"),
                        {"total_count": 1, "workflow_runs": []}):
            with self.subTest(failure=type(failure).__name__):
                scenario = Scenario([run(3, "candidate", 0), run(1, "tested", 5, tested=True)])
                scenario.http.add("/actions/workflows/1/runs?event=push&branch=main&per_page=1", failure)
                scenario.execute()
                self.assertEqual([ROOT + "/actions/runs/3/rerun"], scenario.http.posts())
                self.assertTrue(any("per_page=100" in request.full_url and "/workflows/1/runs?" in request.full_url
                                    for request in scenario.http.calls))
                self.assertTrue(any("fetch" in arguments for arguments, _ in scenario.graph.calls))

    def test_malformed_exact_attempt_timestamp_falls_back_then_fails_full_history(self):
        for started_at in (None, 17):
            with self.subTest(started_at=started_at):
                record = run(3, "candidate", 0, tested=True, attempt=2)
                scenario = Scenario([record])
                scenario.attempt(scenario.rows[3], 2, record[1], run_started_at=started_at)
                with self.assertRaises((ValueError, TypeError, AttributeError)):
                    scenario.execute()
                resources = [request.full_url for request in scenario.http.calls]
                self.assertIn(ROOT + "/actions/workflows/1/runs?event=push&branch=main&per_page=100", resources)
                self.assertEqual(2, resources.count(ROOT + "/actions/runs/3/attempts/2"))
                self.assertEqual([], scenario.http.posts())

    def test_candidate_run_or_newest_push_changing_after_coverage_uses_full_history(self):
        for changed_read in ("run", "newest"):
            with self.subTest(changed_read=changed_read):
                records = [run(3, "candidate", 5, tested=True)]
                if changed_read == "newest":
                    records.insert(0, run(4, "replacement", 0))
                scenario = Scenario(records)
                row = scenario.rows[3]
                replacement = (scenario.rows[4] if changed_read == "newest" else
                               {**row, "run_attempt": 2, "status": "in_progress", "conclusion": None})
                resource = "/actions/workflows/1/runs?event=push&branch=main&per_page=1"
                reads = 0
                def newest(request):
                    nonlocal reads
                    reads += 1
                    return {"total_count": 1, "workflow_runs": [replacement if reads > 1 else row]}
                if changed_read == "run":
                    scenario.http.add("/actions/runs/3", replacement)
                else:
                    scenario.http.add(resource, newest)
                scenario.execute()
                expected = [ROOT + "/actions/runs/4/rerun"] if changed_read == "newest" else []
                self.assertEqual(expected, scenario.http.posts())
                self.assertTrue(any("per_page=100" in request.full_url and "/workflows/1/runs?" in request.full_url
                                    for request in scenario.http.calls))

    def test_recorded_pending_push_waits_without_attempt_or_rerun_requests(self):
        scenario = Scenario([(row, []) for row in RECORDED["runs"]["workflow_runs"]], workflows=[(10002, "Tests")])
        scenario.execute(**{"test-workflows": "Tests"})
        self.assertEqual([], scenario.http.posts())
        self.assertEqual(1, len(scenario.summaries))

    def test_no_new_merge_no_history_and_not_due_make_no_rerun(self):
        for records in ([], [run(1, "a", 20, tested=True)], [run(2, "b", 1), run(1, "a", 2, tested=True)]):
            with self.subTest(records=records):
                scenario = Scenario(records)
                scenario.execute()
                self.assertEqual([], scenario.http.posts())
                self.assertEqual(1, len(scenario.summaries))

    def test_no_test_due_interval_and_failed_test_rerun_only_newest_push(self):
        for older in (None, run(1, "a", 4, tested=True), run(1, "a", 1, tested=True, conclusion="failure")):
            with self.subTest(older=older):
                scenario = Scenario([run(3, "c", 0), run(2, "b", .5)] + ([older] if older else []))
                scenario.execute()
                self.assertEqual([ROOT + "/actions/runs/3/rerun"], scenario.http.posts())
                self.assertEqual(1, len(scenario.summaries))

    def test_active_test_and_newest_queued_attempt_wait(self):
        for records in ([run(3, "c", 0), run(1, "a", 10, tested=True, status="in_progress", conclusion=None)],
                        [run(3, "c", 0, status="queued", conclusion=None)]):
            with self.subTest(records=records):
                scenario = Scenario(records)
                scenario.execute()
                self.assertEqual([], scenario.http.posts())
                self.assertTrue(any("per_page=100" in request.full_url and "/workflows/1/runs?" in request.full_url
                                    for request in scenario.http.calls))

    def test_cancelled_latest_attempt_retains_earlier_test_and_its_start_clock(self):
        for original_start, expected in ((2, []), (5, [ROOT + "/actions/runs/3/rerun"])):
            with self.subTest(original_start=original_start):
                old = run(1, "a", 20, tested=True, attempt=3, conclusion="cancelled", started=.25)
                scenario = Scenario([run(3, "c", 0), old])
                scenario.attempt(old[0], 2, old[1], conclusion="success", run_started_at=clock(original_start))
                scenario.execute()
                self.assertEqual(expected, scenario.http.posts())
        newest = run(3, "c", 20, tested=True, attempt=3, conclusion="cancelled", started=.25)
        scenario = Scenario([newest])
        scenario.attempt(newest[0], 2, newest[1], conclusion="success", run_started_at=clock(5))
        scenario.execute()
        self.assertEqual([], scenario.http.posts())
        self.assertTrue(any("per_page=100" in request.full_url and "/workflows/1/runs?" in request.full_url
                            for request in scenario.http.calls))

    def test_latest_test_is_chosen_by_attempt_start_not_push_arrival(self):
        old = run(1, "a", 20, tested=True, attempt=2, started=1)
        scenario = Scenario([run(3, "c", 0), run(2, "b", 5, tested=True, conclusion="failure"), old])
        scenario.execute()
        self.assertEqual([], scenario.http.posts())

    def test_older_tested_tip_covers_newest_push_despite_newer_divergent_test(self):
        for newest_sha, ancestry in (("a", {}), ("parent", {"a": ["parent"]})):
            with self.subTest(newest_sha=newest_sha):
                scenario = Scenario([run(3, newest_sha, 0), run(2, "b", 5, tested=True),
                                     run(1, "a", 20, tested=True)], ancestry=ancestry)
                scenario.execute()
                self.assertEqual([], scenario.http.posts())

    def test_divergent_tested_roots_share_one_git_snapshot_without_fetching_candidate(self):
        scenario = Scenario([run(3, "c", 0), run(2, "b", 5, tested=True), run(1, "a", 20, tested=True)])
        scenario.execute()
        self.assertEqual([ROOT + "/actions/runs/3/rerun"], scenario.http.posts())
        calls = scenario.graph.calls
        fetches = [(args, options) for args, options in calls if "fetch" in args]
        self.assertTrue(fetches)
        roots = {oid for _, options in fetches for oid in options["input"].splitlines()}
        self.assertEqual({scenario.graph.identities["a"], scenario.graph.identities["b"]}, roots)
        self.assertEqual(1, len({args[2] for args, _ in calls}))
        self.assertFalse(Path(scenario.materialization).exists())

    def test_non_test_skipped_and_unstarted_jobs_supply_no_coverage(self):
        for variant in ("excluded", "skipped", "unstarted", "cancelled"):
            with self.subTest(variant=variant):
                record = run(3, "c", 1, tested=True, conclusion="cancelled" if variant == "cancelled" else "success")
                job = record[1][-1]
                if variant == "skipped":
                    job["conclusion"] = "skipped"
                if variant == "unstarted":
                    job["started_at"] = None
                scenario = Scenario([record])
                scenario.execute(**{"non-test-jobs": job["name"] if variant == "excluded" else ""})
                self.assertEqual([ROOT + "/actions/runs/3/rerun"], scenario.http.posts())

    def test_workflows_are_evaluated_independently_and_report_each_once(self):
        for requested in ("Other\nTests\nOther", "Tests\nOther\nTests"):
            with self.subTest(requested=requested):
                scenario = Scenario([run(4, "parent", 0, workflow=2), run(5, "c", 1, tested=True, workflow=2),
                                     run(3, "c", 0), run(1, "a", 20, tested=True)],
                                    workflows=[(2, "Other"), (1, "Tests")], ancestry={"c": ["parent"]})
                scenario.execute(**{"test-workflows": requested})
                self.assertEqual([ROOT + "/actions/runs/3/rerun"], scenario.http.posts())
                self.assertEqual(2, len(scenario.summaries))

    def test_listed_first_attempt_racing_graphql_second_attempt_uses_exact_refreshed_evidence(self):
        record = run(3, "c", 20, tested=True)
        scenario = Scenario([record])
        row = scenario.rows[3]
        refreshed = {**row, "run_attempt": 2, "conclusion": "cancelled", "run_started_at": clock(1)}
        scenario.snapshots[row["check_suite_node_id"]] = refreshed
        scenario.http.add("/actions/runs/3", refreshed)
        scenario.attempt(row, 1, record[1], conclusion="cancelled")
        scenario.attempt(refreshed, 2, [], conclusion="cancelled")
        scenario.execute()
        self.assertEqual([ROOT + "/actions/runs/3/rerun"], scenario.http.posts())
        resources = {request.full_url for request in scenario.http.calls}
        self.assertIn(ROOT + "/actions/runs/3", resources)
        self.assertIn(ROOT + "/actions/runs/3/attempts/2", resources)
        self.assertIn(ROOT + "/actions/runs/3/attempts/1", resources)
        self.assertEqual([], scenario.graph.calls)

    def test_same_attempt_lifecycle_refresh_prevents_cancelled_coverage_and_active_final_result(self):
        for listed_status, listed_conclusion, fresh_status, fresh_conclusion, expected in (
                ("completed", "success", "completed", "cancelled", [ROOT + "/actions/runs/3/rerun"]),
                ("completed", "failure", "in_progress", None, [])):
            with self.subTest(fresh_status=fresh_status):
                scenario = Scenario([run(3, "c", 1, tested=True, status=listed_status, conclusion=listed_conclusion)])
                scenario.snapshots[scenario.rows[3]["check_suite_node_id"]].update(
                    status=fresh_status, conclusion=fresh_conclusion)
                scenario.execute()
                self.assertEqual(expected, scenario.http.posts())

    def test_run_becoming_active_during_timing_or_coverage_discovery_prevents_rerun(self):
        for discovery in ("timing", "coverage"):
            with self.subTest(discovery=discovery):
                records = [run(100, "candidate", 0), run(99, "tested", 5, tested=True)]
                if discovery == "coverage":
                    records += [run(identifier, f"skipped-{identifier}", 6 + (98 - identifier))
                                for identifier in range(98, 80, -1)]
                records += [run(1, "late-active", 40, tested=True)]
                scenario = Scenario(records)
                scenario.snapshots[scenario.rows[1]["check_suite_node_id"]].update(status="IN_PROGRESS", conclusion=None)
                scenario.execute()
                self.assertEqual([], scenario.http.posts())
                self.assertEqual(2 if discovery == "timing" else 3,
                                 sum(request.full_url.endswith("/graphql") for request in scenario.http.calls))

    def test_latest_exact_attempt_state_blocks_rerun_despite_stale_completed_run_and_suite(self):
        for discovery in ("timing", "coverage"):
            for state in ("queued", "in_progress"):
                with self.subTest(discovery=discovery, state=state):
                    records = [run(100, "candidate", 0), run(99, "tested", 5, tested=True)]
                    if discovery == "coverage":
                        records += [run(identifier, f"skipped-{identifier}", 6 + (98 - identifier))
                                    for identifier in range(98, 80, -1)]
                    records += [run(1, "late-active", 40, tested=True, attempt=2,
                                    started=1 if discovery == "timing" else 40)]
                    scenario = Scenario(records)
                    row = scenario.rows[1]
                    jobs = copy.deepcopy(records[-1][1])
                    jobs[-1].update(status=state, conclusion=None,
                                    started_at=clock(1) if state == "in_progress" else None)
                    scenario.attempt(row, 2, jobs, status=state, conclusion=None)
                    scenario.attempt(row, 1, records[-1][1], run_started_at=clock(41))
                    scenario.execute()
                    self.assertEqual([], scenario.http.posts())
                    self.assertTrue(any(request.full_url == ROOT + "/actions/runs/1/attempts/2"
                                        for request in scenario.http.calls))

    def test_refreshed_attempt_start_reorders_timing_before_older_failure_can_trigger_rerun(self):
        records = [run(100, "candidate", 0), run(99, "failure", 5, tested=True, conclusion="failure")]
        records += [run(identifier, f"skipped-{identifier}", 6 + (98 - identifier))
                    for identifier in range(98, 90, -1)]
        records += [run(1, "stale", 20, tested=True)]
        scenario = Scenario(records)
        refreshed = {**scenario.rows[1], "run_attempt": 2, "run_started_at": clock(1)}
        scenario.snapshots[refreshed["check_suite_node_id"]] = refreshed
        scenario.http.add("/actions/runs/1", refreshed)
        scenario.attempt(refreshed, 2, records[-1][1])
        scenario.execute()
        self.assertEqual([], scenario.http.posts())
        self.assertTrue(any("/actions/runs/1/attempts/2/jobs" in request.full_url for request in scenario.http.calls))

    def test_matching_single_attempt_positive_needs_no_exact_attempt_requests(self):
        scenario = Scenario([run(3, "c", 1, tested=True)])
        scenario.execute()
        self.assertEqual([], scenario.http.posts())
        self.assertFalse(any("/attempts/" in request.full_url for request in scenario.http.calls))

    def test_all_checks_second_page_retains_earlier_positive_after_cancelled_rerun(self):
        record = run(3, "c", 20, attempt=2, conclusion="cancelled")
        scenario = Scenario([record])
        row = scenario.rows[3]
        earlier = run(3, "c", 20, tested=True)[1]
        scenario.attempt(row, 1, earlier, conclusion="success", run_started_at=clock(5))
        node = scenario.suite(row["check_suite_node_id"])
        skipped = {"name": "test", "status": "COMPLETED", "conclusion": "SKIPPED", "startedAt": clock(1)}
        node["checkRuns"]["nodes"] = [
            {**skipped, "detailsUrl": f"https://github.com/owner/repo/actions/runs/3/job/{10000 + index}"}
            for index in range(129)] + [{**skipped, "conclusion": "SUCCESS",
                                         "detailsUrl": "https://github.com/owner/repo/actions/runs/3/job/99999"}]
        node["checkRuns"]["totalCount"] = 130
        scenario.suite_overrides[row["check_suite_node_id"]] = node
        scenario.execute()
        self.assertEqual([], scenario.http.posts())
        self.assertEqual(2, sum(request.full_url.endswith("/graphql") for request in scenario.http.calls))
        self.assertTrue(any("/attempts/1/jobs" in request.full_url for request in scenario.http.calls))

    def test_replacement_merge_is_the_next_hourly_target(self):
        first = Scenario([run(2, "b", 1)])
        first.execute()
        second = Scenario([run(3, "c", 0), run(2, "b", 1, attempt=2, conclusion="cancelled")])
        second.attempt(second.http.routes[second.http.key("GET", ROOT + "/actions/runs/2/attempts/2")][0], 1, [])
        second.execute()
        self.assertEqual([ROOT + "/actions/runs/2/rerun"], first.http.posts())
        self.assertEqual([ROOT + "/actions/runs/3/rerun"], second.http.posts())


class StoppedTests(unittest.TestCase):
    def test_first_quiet_merge_and_second_minutes_later_pass(self):
        for records in ([run(3, "c", 0)], [run(3, "c", 0), run(2, "b", .1), run(1, "a", 40, tested=True)]):
            with self.subTest(records=records):
                self.assertFalse(Scenario(records).execute(operation="decide"))

    def test_strict_three_interval_boundary_and_continuous_merges_preserve_oldest_age(self):
        for age, overdue in ((12, False), (12.001, True), (40, True)):
            with self.subTest(age=age):
                scenario = Scenario([run(3, "c", 0), run(2, "b", .1), run(1, "a", age)])
                if overdue:
                    with self.assertRaises(ValueError):
                        scenario.execute(operation="decide")
                else:
                    self.assertFalse(scenario.execute(operation="decide"))

    def test_duplicate_pushes_keep_original_commit_arrival(self):
        scenario = Scenario([run(3, "c", 0), run(2, "a", 1), run(1, "a", 20)])
        with self.assertRaises(ValueError):
            scenario.execute(operation="decide")

    def test_attempt_created_at_cannot_reset_original_push_arrival(self):
        for changed_attempt in (1, 2):
            with self.subTest(changed_attempt=changed_attempt):
                record = run(1, "a", 20, tested=True, attempt=2, conclusion="cancelled", started=1)
                scenario = Scenario([run(3, "c", 0), record])
                scenario.attempt(scenario.rows[1], 1, [], conclusion="success", run_started_at=clock(1),
                                 created_at=clock(1) if changed_attempt == 1 else clock(20))
                scenario.attempt(scenario.rows[1], 2, record[1], conclusion="cancelled", run_started_at=clock(1),
                                 created_at=clock(1) if changed_attempt == 2 else clock(20))
                with self.assertRaises(ValueError):
                    scenario.execute(operation="decide")
                self.assertTrue(any("/actions/runs/1/attempts/1/jobs" in request.full_url for request in scenario.http.calls))
                self.assertEqual([], scenario.http.posts())

    def test_same_sha_current_push_does_not_hide_earlier_overdue_arrival(self):
        scenario = Scenario([run(3, "c", 0), run(1, "c", 20)])
        with self.assertRaises(ValueError):
            scenario.execute(operation="decide")

    def test_current_push_run_is_excluded_even_when_its_record_is_old(self):
        self.assertFalse(Scenario([run(3, "c", 100)]).execute(operation="decide"))

    def test_ancestral_test_covers_old_skips_but_newer_unrelated_test_does_not(self):
        for ancestors, overdue in (({"b": ["a"]}, False), ({}, True)):
            with self.subTest(ancestors=ancestors):
                scenario = Scenario([run(3, "c", 0), run(2, "b", 1, tested=True), run(1, "a", 20)], ancestry=ancestors)
                if overdue:
                    with self.assertRaises(ValueError):
                        scenario.execute(operation="decide")
                else:
                    self.assertFalse(scenario.execute(operation="decide"))

    def test_cancelled_test_does_not_cover_old_skip_but_active_test_does(self):
        for state, conclusion, overdue in (("completed", "cancelled", True), ("in_progress", None, False)):
            with self.subTest(state=state, conclusion=conclusion):
                scenario = Scenario([run(3, "c", 0), run(2, "b", 1, tested=True, status=state, conclusion=conclusion),
                                     run(1, "a", 20)], ancestry={"b": ["a"]})
                if overdue:
                    with self.assertRaises(ValueError):
                        scenario.execute(operation="decide")
                else:
                    self.assertFalse(scenario.execute(operation="decide"))

    def test_unknown_ambiguous_workflow_or_absent_current_run_fails_closed(self):
        for scenario in (Scenario(workflows=[(2, "Other")]), Scenario(workflows=[(1, "Tests"), (2, "Tests")]), Scenario()):
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                scenario.execute(operation="decide")


class ApiContract(unittest.TestCase):
    def setUp(self):
        self.http = Transport()
        self.api = cadence.GitHub("https://api.github.com", "owner/repo", TOKEN)
        self.patcher = patch.object(cadence, "urlopen", self.http)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_jobs_and_workflows_follow_link_headers(self):
        for endpoint, field, method in (("/actions/workflows", "workflows", self.api.workflows),
                                       ("/actions/runs/1/attempts/2/jobs", "jobs", lambda: self.api.jobs(1, 2))):
            with self.subTest(endpoint=endpoint):
                self.http.add(endpoint + "?per_page=100", {"total_count": 2, field: [{"id": 1}]},
                              links=f'<{ROOT}{endpoint}?per_page=100&page=2>; rel="next"')
                self.http.add(endpoint + "?per_page=100&page=2", {"total_count": 2, field: [{"id": 2}]})
                self.assertEqual([{"id": 1}, {"id": 2}], method())

    def test_canonical_repository_id_pagination_verifies_identity_before_following(self):
        endpoint = "/actions/workflows?per_page=100"
        canonical = "https://api.github.com/repositories/123456/actions/workflows?per_page=100&page=2"
        self.http.add("", {"id": 123456})
        self.http.add(endpoint, {"total_count": 2, "workflows": [{"id": 1}]},
                      links=f'<{canonical}>; rel="next"')
        self.http.routes[self.http.key("GET", canonical)] = ({"total_count": 2, "workflows": [{"id": 2}]}, "")
        self.assertEqual([{"id": 1}, {"id": 2}], self.api.workflows())
        for untrusted in (canonical.replace("123456", "999999"), canonical.replace("api.github.com", "other.invalid")):
            with self.subTest(untrusted=untrusted):
                self.http.add(endpoint, {"total_count": 2, "workflows": [{"id": 1}]},
                              links=f'<{untrusted}>; rel="next"')
                self.http.calls = []
                with self.assertRaises(ValueError):
                    self.api.workflows()
                self.assertNotIn(untrusted, [request.full_url for request in self.http.calls])

    def test_capped_runs_page_into_older_windows_and_keep_timestamp_boundary_rows(self):
        rows = [run(identifier, str(identifier), identifier / 10)[0] for identifier in range(1, 1001)]
        resource = "/actions/workflows/1/runs?event=push&branch=main&per_page=100"
        for page in range(1, 11):
            endpoint = resource + (f"&page={page}" if page > 1 else "")
            self.http.add(endpoint, {"total_count": 1002, "workflow_runs": rows[(page - 1) * 100:page * 100]},
                          links=f'<{ROOT}{resource}&page={page + 1}>; rel="next"' if page < 10 else "")
        boundary = rows[-1]["created_at"]
        tied = run(1001, "1001", 100)[0]
        older = run(1002, "1002", 101)[0]
        self.http.add(resource + "&" + urlencode({"created": boundary}),
                      {"total_count": 2, "workflow_runs": [rows[-1], tied]})
        self.http.add(resource + "&" + urlencode({"created": "<" + boundary}),
                      {"total_count": 1, "workflow_runs": [older]})
        result = self.api.runs(1, "main")
        self.assertEqual(set(range(1, 1003)), {row["id"] for row in result})
        self.assertEqual(1002, result[-1]["id"])

    def test_truncated_repeated_and_malformed_pages_fail_instead_of_clean_history(self):
        resource = "/actions/workflows?per_page=100"
        cases = [({"total_count": 2, "workflows": [{"id": 1}]}, ""),
                 ({"total_count": 2, "workflows": [{"id": 1}]}, f'<{ROOT}{resource}>; rel="next"'),
                 ({"total_count": 1}, "")]
        for payload, links in cases:
            with self.subTest(payload=payload, links=links), self.assertRaises(ValueError):
                self.http.add(resource, payload, links=links)
                self.api.workflows()

    def test_invalid_totals_and_duplicate_run_pages_cannot_hide_history(self):
        resource = "/actions/workflows/1/runs?event=push&branch=main&per_page=100"
        newest = run(3, "newest", 0)[0]
        older = run(1, "overdue", 20)[0]
        cases = [({"total_count": total, "workflow_runs": []}, None)
                 for total in (-1, True, 1.5, "0", None)]
        cases += [({"total_count": 1, "workflow_runs": [row]}, None)
                  for row in ({}, {"id": True}, {"id": "1"}, {"id": 0}, {"id": -1}, None)]
        cases += [({"total_count": 1, "workflow_runs": [newest, older]}, None),
                  ({"total_count": 2, "workflow_runs": [newest]},
                   {"total_count": 1, "workflow_runs": [older]}),
                  ({"total_count": 2, "workflow_runs": [newest]},
                   {"total_count": 3, "workflow_runs": [older]}),
                  ({"total_count": 2, "workflow_runs": [newest]},
                   {"total_count": 2, "workflow_runs": [newest]})]
        for first, second in cases:
            with self.subTest(first=first, second=second):
                self.http.add(resource, first,
                              links=f'<{ROOT}{resource}&page=2>; rel="next"' if second else "")
                if second:
                    self.http.add(resource + "&page=2", second)
                with self.assertRaises(ValueError):
                    self.api.runs(1, "main")

    def test_rerun_races_defer_only_when_refetch_confirms_progress(self):
        row = run(1, "a", 1, attempt=2)[0]
        valid = (({**row, "status": "in_progress"}, True),
                 ({**row, "run_attempt": 3}, True), (row, False))
        invalid = [None, [], {}, {**row, "id": 99}, {**row, "id": True},
                   {**row, "id": "1"}, {**row, "status": "unknown"}, {**row, "status": None},
                   {**row, "run_attempt": "3"}, {**row, "run_attempt": True},
                   {**row, "run_attempt": 1}, {**row, "run_attempt": 1, "status": "in_progress"}]
        cases = [(code, current, deferred) for code in (409, 422)
                 for current, deferred in (*valid, *((current, False) for current in invalid))]
        cases.append((403, row, False))
        for code, current, deferred in cases:
            with self.subTest(code=code, current=current):
                self.http.add("/actions/runs/1/rerun", HTTPError(ROOT, code, TOKEN, {}, None), method="POST")
                self.http.add("/actions/runs/1", current)
                if deferred:
                    self.assertFalse(self.api.rerun(row))
                else:
                    with self.assertRaises(cadence.ApiError) as failure:
                        self.api.rerun(row)
                    self.assertEqual(code, failure.exception.code)
                    self.assertIn("POST /repos/owner/repo/actions/runs/1/rerun", str(failure.exception))
                    self.assertNotIn(TOKEN, str(failure.exception))

    def test_transport_uses_auth_header_and_errors_do_not_disclose_token(self):
        self.http.add("/actions/workflows?per_page=100", {"total_count": 0, "workflows": []})
        self.api.workflows()
        request = self.http.calls[-1]
        self.assertEqual("Bearer " + TOKEN, request.get_header("Authorization"))
        self.assertNotIn(TOKEN, request.full_url)
        for error in (URLError(TOKEN), HTTPError(ROOT, 403, "upstream text " + TOKEN, {}, None)):
            with self.subTest(error=type(error).__name__):
                self.http.add("/actions/workflows?per_page=100", error)
                with self.assertRaises(ValueError) as failure:
                    self.api.workflows()
                self.assertNotIn(TOKEN, str(failure.exception))
                self.assertNotIn("upstream text", str(failure.exception))
                if isinstance(error, HTTPError):
                    self.assertIn("GET /repos/owner/repo/actions/workflows", str(failure.exception))


class BatchedCheckContract(unittest.TestCase):
    def test_recorded_all_check_connection_round_trips_both_attempts_job_identities(self):
        fixture = RECORDED["all_checks"]
        node = copy.deepcopy(fixture["suite"])
        identifier = fixture["jobs"][0]["run_id"]
        node.update(status="COMPLETED", conclusion="FAILURE",
                    workflowRun={"databaseId": identifier, "runAttempt": 2})
        http = Transport()
        def graphql(query):
            self.assertRegex(query, r"filterBy\s*:\s*\{\s*checkType\s*:\s*ALL\s*\}")
            self.assertIn("nodes(ids:", query)
            return {"data": {"nodes": [node]}}
        http.graphql = graphql
        with patch.object(cadence, "urlopen", http):
            api = cadence.GitHub("https://api.github.com", "owner/repo", TOKEN)
            result = api.checks([{"id": identifier, "check_suite_node_id": node["id"]}])[identifier]
        expected = {str(job["id"]): job for job in fixture["jobs"]}
        self.assertEqual({1, 2}, {job["run_attempt"] for job in expected.values()})
        self.assertEqual(expected.keys(), result["jobs"].keys())
        for key, job in result["jobs"].items():
            self.assertEqual({field: expected[key][field] for field in ("name", "status", "conclusion", "started_at")}, job)

    def test_partial_null_errored_and_mismatched_check_history_fails_before_rerun(self):
        cases = ("null", "missing-selection", "errors", "partial", "suite-identity", "workflow-identity",
                 "job-identity", "job-lifecycle", "suite-lifecycle", "missing-start", "missing-page-info",
                 "invalid-attempt", "invalid-count")
        for continuation in (False, True):
            for defect in cases:
                with self.subTest(continuation=continuation, defect=defect):
                    scenario = Scenario([run(3, "c", 1)])
                    scenario.http.add("/actions/workflows/1/runs?event=push&branch=main&per_page=1",
                                      URLError("candidate lookup unavailable"))
                    key = scenario.rows[3]["check_suite_node_id"]
                    node = scenario.suite(key)
                    node["checkRuns"]["pageInfo"] = {"hasNextPage": False, "endCursor": None}
                    first_page = copy.deepcopy(node)
                    first_page["checkRuns"].update(nodes=[], pageInfo={"hasNextPage": True, "endCursor": "next"})
                    connection = node["checkRuns"]
                    if defect == "null":
                        node = None
                    elif defect == "suite-identity":
                        node["id"] = "CS_another_suite"
                    elif defect == "workflow-identity":
                        node["workflowRun"]["databaseId"] = 999
                    elif defect == "job-identity":
                        connection["nodes"][0]["detailsUrl"] = "https://github.com/owner/repo/actions/runs/999/job/1"
                    elif defect == "job-lifecycle":
                        connection["nodes"][0]["conclusion"] = None
                    elif defect == "suite-lifecycle":
                        del node["conclusion"]
                    elif defect == "missing-start":
                        del connection["nodes"][0]["startedAt"]
                    elif defect == "missing-page-info":
                        del connection["pageInfo"]
                    elif defect == "invalid-attempt":
                        node["workflowRun"]["runAttempt"] = True
                    elif defect == "invalid-count":
                        connection["totalCount"] = True
                    elif defect == "partial":
                        connection["nodes"] = []
                    calls = 0
                    def graphql(query):
                        nonlocal calls
                        calls += 1
                        if continuation and calls == 1:
                            return {"data": {"nodes": [first_page]}}
                        payload = {"data": {"s0": node} if continuation else {"nodes": [node]}}
                        if defect == "missing-selection":
                            payload["data"] = {}
                        elif defect == "errors":
                            payload["errors"] = [{"message": "partial backend failure"}]
                        return payload
                    scenario.http.graphql = graphql
                    with self.assertRaises(ValueError):
                        scenario.execute()
                    self.assertEqual(2 if continuation else 1, calls)
                    self.assertEqual([], scenario.http.posts())

    def test_nodes_first_page_requires_submitted_suite_order_and_count(self):
        for defect in ("reversed", "truncated", "extra"):
            with self.subTest(defect=defect):
                scenario = Scenario([run(3, "c", 0), run(1, "a", 5, tested=True)])
                scenario.http.add("/actions/workflows/1/runs?event=push&branch=main&per_page=1",
                                  URLError("candidate lookup unavailable"))
                nodes = [scenario.suite(scenario.rows[identifier]["check_suite_node_id"]) for identifier in (3, 1)]
                for node in nodes:
                    node["checkRuns"]["pageInfo"] = {"hasNextPage": False, "endCursor": None}
                nodes = list(reversed(nodes)) if defect == "reversed" else nodes[:1] if defect == "truncated" else nodes + [nodes[0]]
                scenario.http.graphql = lambda query: {"data": {"nodes": nodes}}
                with self.assertRaises(ValueError):
                    scenario.execute()
                self.assertEqual([], scenario.http.posts())

    def test_repeated_cursor_changed_attempt_or_changed_count_cannot_complete_pagination(self):
        for defect in ("cursor", "attempt", "count"):
            with self.subTest(defect=defect):
                scenario = Scenario([run(3, "c", 1)])
                scenario.http.add("/actions/workflows/1/runs?event=push&branch=main&per_page=1",
                                  URLError("candidate lookup unavailable"))
                node = scenario.suite(scenario.rows[3]["check_suite_node_id"])
                node["checkRuns"].update(totalCount=2, pageInfo={"hasNextPage": True, "endCursor": "same"})
                count = 0
                def graphql(query):
                    nonlocal count
                    count += 1
                    page = copy.deepcopy(node)
                    if count > 1 and defect == "attempt":
                        page["workflowRun"]["runAttempt"] = 2
                    if count > 1 and defect == "count":
                        page["checkRuns"]["totalCount"] = 3
                    return {"data": {"nodes": [page]} if count == 1 else {"s0": page}}
                scenario.http.graphql = graphql
                with self.assertRaises(ValueError):
                    scenario.execute()
                self.assertEqual(2, count)
                self.assertEqual([], scenario.http.posts())


class GitAncestryContract(unittest.TestCase):
    def test_real_divergent_git_graph_covers_only_declared_parents_and_keeps_credentials_in_environment(self):
        import base64
        scenario = Scenario([run(3, "candidate", 0), run(2, "tested", 5, tested=True),
                             run(1, "parent", 20)], ancestry={"tested": ["parent"]})
        scenario.execute()
        self.assertEqual([ROOT + "/actions/runs/3/rerun"], scenario.http.posts())
        secret = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        for arguments, options in scenario.graph.calls:
            self.assertNotIn(TOKEN, " ".join(arguments) + options["input"])
            self.assertNotIn(secret, " ".join(arguments) + options["input"])
            self.assertIn(secret, options["env"]["GIT_CONFIG_VALUE_0"])
            self.assertEqual("http.https://github.com/.extraheader", options["env"]["GIT_CONFIG_KEY_0"])
            self.assertEqual("1", options["env"]["GIT_NO_LAZY_FETCH"])
            if "fetch" in arguments:
                self.assertIn("--filter=tree:0", arguments)
                self.assertIn("--no-tags", arguments)
                self.assertEqual(scenario.graph.identities["tested"] + "\n", options["input"])
        self.assertTrue(scenario.graph.artifacts)
        for artifact in scenario.graph.artifacts:
            self.assertNotIn(TOKEN, artifact)
            self.assertNotIn(secret, artifact)
        self.assertFalse(Path(scenario.materialization).exists())

    def test_git_unavailable_failed_fetch_shallow_or_incomplete_graph_fails_and_cleans_up(self):
        for defect in ("unavailable", "fetch", "shallow", "omitted-tip", "unfiltered"):
            with self.subTest(defect=defect):
                scenario = Scenario([run(3, "c", 0), run(1, "a", 20, tested=True)])
                def failure(arguments, options):
                    if defect == "unavailable":
                        raise FileNotFoundError("git")
                    if defect == "fetch" and "fetch" in arguments:
                        return subprocess.CompletedProcess(arguments, 1, stdout="", stderr=TOKEN)
                    if defect == "shallow" and "--is-shallow-repository" in arguments:
                        return subprocess.CompletedProcess(arguments, 0, stdout="true\n", stderr="")
                    if defect == "omitted-tip" and "rev-list" in arguments:
                        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")
                    if defect == "unfiltered" and "fetch" in arguments:
                        scenario.graph.materialize(scenario.graph.runner_temp)
                        rewritten = [scenario.graph.directory.as_uri() if item == "https://github.com/owner/repo.git" else item
                                     for item in arguments if item != "--filter=tree:0"]
                        return SUBPROCESS_RUN(rewritten, **options)
                scenario.graph.failure = failure
                with self.assertRaises(ValueError) as failure_result:
                    scenario.execute()
                self.assertNotIn(TOKEN, str(failure_result.exception))
                self.assertEqual([], scenario.http.posts())
                self.assertFalse(Path(scenario.materialization).exists())


class HistoryCost(unittest.TestCase):
    def test_covered_newest_push_has_bounded_requests_without_history_or_git_reads(self):
        for attempt in (1, 2):
            counts = []
            for retained in (1, 1923):
                with self.subTest(attempt=attempt, retained=retained):
                    records = [run(1923, "covered", 0, tested=True, attempt=attempt,
                                   conclusion="failure" if attempt == 2 else "success")]
                    records += [run(identifier, f"older-{identifier}", 1 + (1923 - identifier) / 100)
                                for identifier in range(1922, 1923 - retained, -1)]
                    scenario = Scenario(records)
                    for key in list(scenario.http.routes):
                        if key[1].endswith("/workflows/1/runs") and ("per_page", ("100",)) in key[2]:
                            del scenario.http.routes[key]
                    def forbidden_git(arguments, options):
                        raise AssertionError("Covered newest push must not read a commit graph")
                    scenario.graph.failure = forbidden_git
                    scenario.execute()
                    self.assertEqual([], scenario.http.posts())
                    self.assertEqual([], scenario.graph.calls)
                    resources = [request.full_url for request in scenario.http.calls]
                    self.assertEqual(2, resources.count(ROOT + "/actions/workflows/1/runs?event=push&branch=main&per_page=1"))
                    self.assertIn("https://api.github.com/graphql", resources)
                    self.assertIn(ROOT + "/actions/runs/1923", resources)
                    if attempt == 2:
                        self.assertIn(ROOT + "/actions/runs/1923/attempts/2", resources)
                        self.assertIn(ROOT + "/actions/runs/1923/attempts/2/jobs?per_page=100", resources)
                    self.assertLessEqual(len(resources), 7)
                    self.assertEqual(1, len(scenario.summaries))
                    counts.append(len(resources))
            self.assertEqual(counts[0], counts[1])

    def test_projected_1923_skipped_only_runs_stay_below_rest_budget(self):
        records = [run(identifier, f"commit-{identifier}", (1924 - identifier) / 100)
                   for identifier in range(1923, 0, -1)]
        scenario = Scenario(records)
        scenario.execute()
        self.assertEqual([ROOT + "/actions/runs/1923/rerun"], scenario.http.posts())
        rest = [request for request in scenario.http.calls if not request.full_url.endswith("/graphql")]
        graphql = [request for request in scenario.http.calls if request.full_url.endswith("/graphql")]
        self.assertLessEqual(len(rest), 1000)
        self.assertEqual(98, len(graphql))
        self.assertFalse(any("/attempts/" in request.full_url for request in rest))
        self.assertEqual([], scenario.graph.calls)

    def test_projected_1923_accumulated_second_attempt_ancestors_stay_below_rest_budget(self):
        records = [run(1923, "candidate", 0)] + [
            run(identifier, f"commit-{identifier}", 5 + (1923 - identifier) / 100, tested=True, attempt=2)
            for identifier in range(1922, 0, -1)]
        ancestry = {"commit-1922": [f"commit-{identifier}" for identifier in range(1, 1922)]}
        scenario = Scenario(records, ancestry=ancestry)
        scenario.execute()
        self.assertEqual([ROOT + "/actions/runs/1923/rerun"], scenario.http.posts())
        rest = [request for request in scenario.http.calls if not request.full_url.endswith("/graphql")]
        self.assertLessEqual(len(rest), 1000)
        self.assertEqual(2, sum("/attempts/" in request.full_url for request in rest))
        self.assertEqual(2, sum(request.full_url.endswith("/graphql") for request in scenario.http.calls))
        fetches = [options for arguments, options in scenario.graph.calls if "fetch" in arguments]
        self.assertEqual(1, len(fetches))
        self.assertEqual(scenario.graph.identities["commit-1922"] + "\n", fetches[0]["input"])


class RunnerContract(unittest.TestCase):
    def test_main_reads_action_inputs_and_writes_machine_output_without_state(self):
        for enabled, expected in (("true", "run_ci=true\n"), ("FALSE", "run_ci=false\n")):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                event = root / "event.json"
                event.write_text(json.dumps({"repository": {"default_branch": "main"}}))
                environment = {"INPUT_OPERATION": "decide", "INPUT_POST_MERGE_CI": enabled,
                               "INPUT_CADENCE": "newest-merge", "GITHUB_EVENT_PATH": str(event),
                               "GITHUB_REPOSITORY": "owner/repo", "GITHUB_EVENT_NAME": "push",
                               "GITHUB_WORKFLOW": "Tests", "GITHUB_OUTPUT": str(root / "output"),
                               "GITHUB_STEP_SUMMARY": str(root / "summary")}
                with patch.dict(os.environ, environment, clear=True), patch.object(cadence, "urlopen", side_effect=AssertionError("network")), patch("sys.stdout", new_callable=io.StringIO):
                    cadence.main()
                self.assertEqual(expected, (root / "output").read_text())
                self.assertLessEqual({item.name for item in root.iterdir()}, {"event.json", "output", "summary"})


if __name__ == "__main__":
    unittest.main()
