"""Census against recorded-style job pages and a stand-in census writer that records its argv and environment."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import census

API = "https://api.github.com"
REPOSITORY = "tommygwu/consumer"
REPOSITORY_ID = 4242
RUN_ID, ATTEMPT = 1001, 2
PR_HEAD, MERGE_SHA = "1" * 40, "2" * 40
FIRST_PAGE = f"{API}/repos/{REPOSITORY}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs?per_page=100"
SECOND_PAGE = f"{API}/repositories/{REPOSITORY_ID}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs?per_page=100&page=2"
JOB_IDS = {"test": "test", "Toolkit end to end": "toolkit-e2e"}
NEEDS = {"test": {"result": "success", "outputs": {}}, "toolkit-e2e": {"result": "success", "outputs": {}}}
CENSUS_WRITER = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["FAKE_CENSUS_LOG"], "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "tested_base": os.environ.get("OPENCLAW_CI_TESTED_BASE")}}) + "\\n")
sys.exit(int(os.environ.get("FAKE_CENSUS_EXIT", "0")))
"""


def job(identifier, name, conclusion, **identity):
    return {"id": identifier, "name": name, "conclusion": conclusion, "run_id": RUN_ID, "head_sha": PR_HEAD,
            "run_attempt": ATTEMPT, **identity}


def recorded_jobs():
    return [job(1, "test", "success"), job(2, "Toolkit end to end (ubuntu-latest, default)", None),
            job(3, "Toolkit end to end (macos-latest, default)", None), job(4, "lint", "skipped"),
            job(5, "test", "failure", run_attempt=1), job(6, "Toolkit census", None)]


class Response:
    def __init__(self, document, link=""):
        self.body = json.dumps(document).encode()
        self.headers = {"Link": link} if link else {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CensusFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.census_dir = self.root / "agents-census"
        self.event = self.root / "event.json"
        self.event.write_text(json.dumps({"pull_request": {"number": 9, "head": {"sha": PR_HEAD}}}), encoding="utf-8")
        writer = self.root / "census-python"
        writer.write_text(CENSUS_WRITER, encoding="utf-8")
        writer.chmod(0o755)
        self.log = self.root / "census-writer.jsonl"
        environment = patch.dict(os.environ, {
            "GITHUB_API_URL": API, "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": str(RUN_ID),
            "GITHUB_RUN_ATTEMPT": str(ATTEMPT), "GITHUB_SHA": MERGE_SHA, "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": str(self.event), "CENSUS_DIR": str(self.census_dir), "INPUT_GITHUB_TOKEN": "job-token",
            "INPUT_NEEDS": json.dumps(NEEDS), "INPUT_JOB_IDS": json.dumps(JOB_IDS),
            "INPUT_CURRENT_JOB": "Toolkit census", "INPUT_REPORT_JOB": "test", "INPUT_EXPECTED_UNITS": "[]",
            "INPUT_REQUIRE_SELECTION": "false", "INPUT_TESTED_BASE": "", "CENSUS_PYTHON": str(writer),
            "CENSUS_AGENTS": str(self.root / "agents"), "FAKE_CENSUS_LOG": str(self.log), "FAKE_CENSUS_EXIT": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.jobs = recorded_jobs()
        self.requests = []
        transport = patch.object(census.Client, "transport", lambda client, request, timeout: self.answer(request))
        transport.start()
        self.addCleanup(transport.stop)

    def answer(self, request):
        self.requests.append(request.full_url)
        pages = {FIRST_PAGE: Response({"total_count": len(self.jobs), "jobs": self.jobs[:3]},
                                      f'<{SECOND_PAGE}>; rel="next", <{SECOND_PAGE}>; rel="last"'),
                 SECOND_PAGE: Response({"total_count": len(self.jobs), "jobs": self.jobs[3:]},
                                       f'<{FIRST_PAGE}>; rel="first", <{FIRST_PAGE}>; rel="prev"'),
                 f"{API}/repos/{REPOSITORY}": Response({"id": REPOSITORY_ID, "full_name": REPOSITORY})}
        if request.full_url not in pages:
            raise AssertionError(f"unexpected GitHub request {request.full_url}")
        return pages[request.full_url]

    def command(self, name, **env):
        err = io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = census.main([name])
        return code, err.getvalue()

    def recorded(self):
        jobs = json.loads((self.census_dir / "jobs.json").read_text(encoding="utf-8"))
        return [(entry["name"], entry["conclusion"]) for entry in jobs]

    def writer_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def write_jobs(self, report_conclusion):
        self.census_dir.mkdir(parents=True, exist_ok=True)
        jobs = [job(6, "Toolkit census", None)]
        if report_conclusion:
            jobs.insert(0, job(1, "test", report_conclusion))
        (self.census_dir / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")


class JobsTest(CensusFixture):
    def test_reads_every_numeric_repository_page_and_fills_lagging_matrix_rows_from_needs(self):
        code, err = self.command("jobs")
        self.assertEqual(0, code, err)
        self.assertEqual([FIRST_PAGE, f"{API}/repos/{REPOSITORY}", SECOND_PAGE], self.requests)
        self.assertEqual([("test", "success"), ("Toolkit end to end (ubuntu-latest, default)", "success"),
                          ("Toolkit end to end (macos-latest, default)", "success"), ("lint", "skipped"),
                          ("Toolkit census", None)], self.recorded())

    def test_empty_job_map_without_a_report_artifact_still_records_every_job(self):
        self.assertEqual(0, self.command("jobs", INPUT_JOB_IDS="{}")[0])
        self.assertEqual([("test", "success"), ("Toolkit end to end (ubuntu-latest, default)", None),
                          ("Toolkit end to end (macos-latest, default)", None), ("lint", "skipped"),
                          ("Toolkit census", None)], self.recorded())
        code, err = self.command("workflow")
        self.assertEqual(0, code, err)
        argv = self.writer_calls()[0]["argv"]
        self.assertEqual(str(self.census_dir / "jobs.json"), argv[argv.index("--jobs-file") + 1])

    def test_legacy_report_artifact_uses_the_original_download_path(self):
        output = self.root / "step-output"
        code, err = self.command("jobs", INPUT_REPORT_ARTIFACT="CI-UNITS-legacy", GITHUB_OUTPUT=str(output))
        self.assertEqual(0, code, err)
        self.assertEqual("legacy-report=true\n", output.read_text())
        self.assertEqual([FIRST_PAGE, f"{API}/repos/{REPOSITORY}", SECOND_PAGE], self.requests)

    def test_push_event_checks_jobs_against_github_sha(self):
        self.jobs = [{**entry, "head_sha": MERGE_SHA} for entry in self.jobs]
        code, err = self.command("jobs", GITHUB_EVENT_NAME="push")
        self.assertEqual(0, code, err)

    def test_job_ids_entry_missing_from_needs_fails(self):
        code, _ = self.command("jobs", INPUT_JOB_IDS=json.dumps({"Toolkit end to end": "e2e"}))
        self.assertEqual(1, code)
        self.assertFalse((self.census_dir / "jobs.json").exists())

    def test_foreign_job_identity_fails(self):
        cases = {"other run": {"run_id": RUN_ID + 1}, "merge commit on a pull request": {"head_sha": MERGE_SHA},
                 "later attempt": {"run_attempt": ATTEMPT + 1}, "attempt zero": {"run_attempt": 0},
                 "string attempt": {"run_attempt": "2"}}
        for case, identity in cases.items():
            with self.subTest(case):
                (self.census_dir / "jobs.json").unlink(missing_ok=True)
                self.jobs = recorded_jobs()
                self.jobs[3] = {**self.jobs[3], **identity}
                self.assertEqual(1, self.command("jobs")[0])
                self.assertFalse((self.census_dir / "jobs.json").exists())

    def test_missing_current_job_fails(self):
        self.jobs = [entry for entry in recorded_jobs() if entry["name"] != "Toolkit census"]
        self.assertEqual(1, self.command("jobs")[0])
        self.jobs = recorded_jobs()
        self.jobs[5] = {**self.jobs[5], "run_attempt": 1}
        self.assertEqual(1, self.command("jobs")[0])


class WorkflowTest(CensusFixture):
    def test_writer_argv_carries_one_expected_unit_flag_per_unit_and_its_failure_fails_the_step(self):
        self.write_jobs("success")
        self.assertEqual(0, self.command("workflow", INPUT_EXPECTED_UNITS='["unit-a", "unit-b"]')[0])
        self.assertEqual([str(self.root / "agents/packages/openclaw-shared/src/openclaw_shared/ci_census.py"),
                          "workflow", "--jobs-file", str(self.census_dir / "jobs.json"),
                          "--report-dir", str(self.census_dir / "reports"), "--report-job", "test",
                          "--current-job", "Toolkit census", "--output-dir", str(self.census_dir / "ci-census"),
                          "--expected-unit", "unit-a", "--expected-unit", "unit-b"], self.writer_calls()[0]["argv"])
        self.assertEqual(1, self.command("workflow", FAKE_CENSUS_EXIT="3")[0])

    def test_tested_base_is_dropped_when_the_report_job_skipped_or_is_absent(self):
        cases = {"success": ("success", "b" * 40), "failure": ("failure", "b" * 40), "skipped": ("skipped", None),
                 "absent": (None, None)}
        for case, (conclusion, expected) in cases.items():
            with self.subTest(case):
                self.log.unlink(missing_ok=True)
                self.write_jobs(conclusion)
                code, err = self.command("workflow", INPUT_TESTED_BASE="b" * 40, OPENCLAW_CI_TESTED_BASE="c" * 40)
                self.assertEqual(0, code, err)
                self.assertEqual(expected, self.writer_calls()[0]["tested_base"])

    def test_require_selection_fails_a_successful_pull_request_report_that_passed_no_selection(self):
        cases = {"pull request without a selection": ("pull_request", "success", "", "true", 1),
                 "pull request with an empty selection": ("pull_request", "success", "[]", "true", 0),
                 "push without a selection": ("push", "success", "", "true", 0),
                 "pull request with units": ("pull_request", "success", '["unit-a"]', "true", 0),
                 "failed report": ("pull_request", "failure", "", "true", 0),
                 "selection not required": ("pull_request", "success", "", "false", 0)}
        for case, (event, conclusion, units, require, expected) in cases.items():
            with self.subTest(case):
                self.log.unlink(missing_ok=True)
                self.write_jobs(conclusion)
                code, _ = self.command("workflow", GITHUB_EVENT_NAME=event, INPUT_EXPECTED_UNITS=units,
                                       INPUT_REQUIRE_SELECTION=require)
                self.assertEqual(expected, code)
                self.assertEqual(1 - expected, len(self.writer_calls()))


if __name__ == "__main__":
    unittest.main()
