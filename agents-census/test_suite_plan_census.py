"""Round-trip a governed-gates result through the agents producer and the composite action's writer."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import skipUnless

from test_census import ATTEMPT, API, MERGE_SHA, PR_HEAD, REPOSITORY, RUN_ID, CensusFixture, Response, job


AGENTS = os.environ.get("CENSUS_TEST_AGENTS")
REPORT_JOB = "Declared Repository Gate"
CURRENT_JOB = "ci-census"
SLUG = "declared-repository-gate"
BASE = "a" * 40
HEAD = {"pull_request": PR_HEAD, "push": MERGE_SHA}
TEST_PATH = "tests/test_selected.py"
REPORT_FILE = f"ci-units-{SLUG}.json"
EXECUTION = {"status": "completed", "started_at": "2026-11-21T20:01:00Z",
             "completed_at": "2026-11-21T20:02:00Z", "steps": [{"name": "Record census units"}]}
ORIGINAL_ARTIFACT = {"id": 108, "name": f"ci-units-{SLUG}-1", "expired": False}
CURRENT_ARTIFACT = {"id": 109, "name": f"ci-units-{SLUG}-{ATTEMPT}", "expired": False}
PRIOR_CENSUS = {"id": 110, "name": "ci-census-workflow-1", "expired": False}


@skipUnless(AGENTS, "the agents checkout is available only to the census end-to-end job")
class SuitePlanCensusTest(CensusFixture):
    def setUp(self):
        super().setUp()
        self.report_dir = self.census_dir / "reports"
        self.host_env = {
            "CENSUS_AGENTS": AGENTS, "CENSUS_PYTHON": sys.executable, "GITHUB_WORKFLOW": "Tests",
            "PYTHONDONTWRITEBYTECODE": "1", "GITHUB_OUTPUT": str(self.root / "step-output"),
            "INPUT_CURRENT_JOB": CURRENT_JOB,
            "INPUT_REPORT_JOB": REPORT_JOB, "INPUT_REPORT_ARTIFACT": f"ci-units-{SLUG}-{ATTEMPT}",
        }
        self.previous = []
        self.artifacts = []
        self.parents = [{"sha": BASE}, {"sha": PR_HEAD}]

    def answer(self, request):
        if request.full_url == f"{API}/repos/{REPOSITORY}/actions/runs/{RUN_ID}/artifacts?per_page=100":
            self.requests.append(request.full_url)
            return Response({"total_count": len(self.artifacts), "artifacts": self.artifacts})
        if request.full_url == f"{API}/repos/{REPOSITORY}/commits/{MERGE_SHA}":
            self.requests.append(request.full_url)
            return Response({"parents": self.parents})
        if request.full_url == f"{API}/repos/{REPOSITORY}/actions/runs/{RUN_ID}/attempts/1/jobs?per_page=100":
            self.requests.append(request.full_url)
            return Response({"total_count": len(self.previous), "jobs": self.previous})
        return super().answer(request)

    def command(self, name, **env):
        return super().command(name, **{**self.host_env, **env})

    def record_jobs(self, event, conclusion, *, other_jobs=(), **execution):
        self.jobs = [
            job(11, REPORT_JOB, conclusion, head_sha=HEAD[event], **execution),
            job(12, CURRENT_JOB, None, head_sha=HEAD[event]),
            *other_jobs,
        ]
        code, err = self.command("jobs", GITHUB_EVENT_NAME=event)
        self.assertEqual(0, code, err)

    def emit_report(self, status, attempt=ATTEMPT, with_skipped=False):
        (self.root / TEST_PATH).parent.mkdir(exist_ok=True)
        (self.root / TEST_PATH).touch()
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"units": [{"id": "unit-tests", "reports": "junit"}, {"id": "entity-registry"}]}))
        result = self.root / "result.json"
        entry = {"id": "unit-tests:unit-tests", "status": status, "targets": [TEST_PATH]}
        if status == "failed":
            entry.update(failure_kind="test", failed_tests=[f"{TEST_PATH}::test_case", "missing.py::test_case"])
        entries = [entry]
        if with_skipped:
            entries.append({"id": "entity-registry", "status": "skipped", "reason": "no-matching-change"})
        result.write_text(json.dumps({"status": status, "units": entries}))
        self.artifacts.append(ORIGINAL_ARTIFACT if attempt == 1 else CURRENT_ARTIFACT)
        subprocess.run([sys.executable, "-B", str(Path(AGENTS) / ".github/scripts/ci_census.py"), "units",
                        "--job", REPORT_JOB, "--result", str(result), "--plan", str(plan),
                        "--repo", str(self.root), "--status", "failure" if status == "failed" else "success",
                        "--out", str(self.report_dir)], check=True, capture_output=True, text=True)
        return json.loads((self.report_dir / REPORT_FILE).read_text())

    def census(self, slug=SLUG, attempt=ATTEMPT):
        return json.loads((self.census_dir / "ci-census" / f"ci-census-{slug}-{attempt}.json").read_text())

    def test_failed_test_file_and_incomplete_collection_survive_a_pr(self):
        expected = self.emit_report("failed")["units"]
        self.record_jobs("pull_request", "failure", **EXECUTION)
        code, err = self.command("workflow")
        self.assertEqual(0, code, err)
        census = self.census()
        self.assertEqual(expected, census["units"])
        unit, = census["units"]
        self.assertEqual("unit-tests:unit-tests", unit["id"])
        file, = unit["files"]
        self.assertEqual(TEST_PATH, file["path"])
        self.assertEqual(1, len(file["cases"]))
        self.assertIs(unit["collection_complete"], False)
        self.assertIs(census["truncated"], False)
        self.assertEqual(
            {"repository": REPOSITORY, "workflow": "Tests", "head_sha": MERGE_SHA, "job": REPORT_JOB,
             "source": "ci", "run_id": RUN_ID, "run_attempt": ATTEMPT, "event": "pull_request",
             "tested_base": BASE, "started_at": EXECUTION["started_at"], "finished_at": EXECUTION["completed_at"]},
            census["run"],
        )

    def test_successful_push_uses_the_new_attempt_and_the_pushed_commit(self):
        self.emit_report("pass")
        self.previous = [job(10, REPORT_JOB, "success", run_attempt=1, head_sha=MERGE_SHA,
                             **{**EXECUTION, "started_at": "2026-11-21T19:00:00Z"})]
        self.record_jobs("push", "success", **EXECUTION)
        self.assertEqual("suite-plan-artifact-id=109\n", (self.root / "step-output").read_text())
        code, err = self.command("workflow", GITHUB_EVENT_NAME="push")
        self.assertEqual(0, code, err)
        census = self.census()
        self.assertEqual("unit-tests:unit-tests", census["units"][0]["id"])
        self.assertIs(census["truncated"], False)
        self.assertEqual((ATTEMPT, MERGE_SHA, "push"),
                         (census["run"]["run_attempt"], census["run"]["head_sha"], census["run"]["event"]))
        self.assertNotIn("tested_base", census["run"])
        self.assertEqual(EXECUTION["started_at"], census["run"]["started_at"])
        self.assertEqual(EXECUTION["completed_at"], census["run"]["finished_at"])

    def test_pr_selection_skip_does_not_truncate_proof_of_another_unit(self):
        expected = self.emit_report("pass", with_skipped=True)["units"]
        self.record_jobs("pull_request", "success", **EXECUTION)
        code, err = self.command("workflow")
        self.assertEqual(0, code, err)
        census = self.census()
        self.assertEqual(expected, census["units"])
        self.assertEqual("skipped:no-matching-change", census["units"][1]["reason"])
        self.assertIs(census["truncated"], False)

    def test_rerun_recovers_the_artifact_and_provenance_of_a_carried_job(self):
        self.emit_report("pass", attempt=1)
        toolkit = job(13, "Shared toolkit", "success", **EXECUTION)
        self.previous = [job(10, REPORT_JOB, "success", run_attempt=1, **EXECUTION),
                         {**toolkit, "id": 9, "run_attempt": 1}]
        self.record_jobs("pull_request", "success", other_jobs=[toolkit], **EXECUTION)
        self.assertEqual("suite-plan-artifact-id=108\n", (self.root / "step-output").read_text())
        code, err = self.command("workflow")
        self.assertEqual(0, code, err)
        report = self.census(attempt=1)
        own = self.census(slug=CURRENT_JOB)
        self.assertEqual(("unit-tests:unit-tests", 1, BASE),
                         (report["units"][0]["id"], report["run"]["run_attempt"], report["run"]["tested_base"]))
        self.assertEqual(EXECUTION["started_at"], report["run"]["started_at"])
        self.assertEqual(EXECUTION["completed_at"], report["run"]["finished_at"])
        self.assertEqual(ATTEMPT, own["run"]["run_attempt"])
        toolkit_run = self.census(slug="shared-toolkit", attempt=1)["run"]
        self.assertEqual((1, BASE), (toolkit_run["run_attempt"], toolkit_run["tested_base"]))
        self.assertEqual({f"ci-census-{SLUG}-1.json", "ci-census-shared-toolkit-1.json",
                          f"ci-census-{CURRENT_JOB}-{ATTEMPT}.json"},
                         {path.name for path in (self.census_dir / "ci-census").iterdir()})

    def test_rerun_does_not_duplicate_an_already_published_census(self):
        units = self.emit_report("pass", attempt=1)["units"]
        (self.report_dir / REPORT_FILE).unlink()
        self.previous = [job(10, REPORT_JOB, "success", run_attempt=1, **EXECUTION)]
        self.artifacts = [PRIOR_CENSUS, {**ORIGINAL_ARTIFACT, "expired": True}]
        prior = self.census_dir / "prior-censuses" / "ci-census-workflow-1"
        prior.mkdir(parents=True)
        census_file = prior / f"ci-census-{SLUG}-1.json"
        body = {"schema": "ci-census", "units": units, "truncated": False, "run": {
            "repository": REPOSITORY, "run_id": RUN_ID, "run_attempt": 1, "head_sha": MERGE_SHA, "job": REPORT_JOB,
            "workflow": "Tests", "event": "pull_request", "source": "ci", "tested_base": BASE,
            "started_at": EXECUTION["started_at"], "finished_at": EXECUTION["completed_at"],
        }}
        self.record_jobs("pull_request", "success", **EXECUTION)
        self.assertEqual("prior-census-ids=110\n", (self.root / "step-output").read_text())
        for invalid in ({**body, "units": [{}]}, {**body, "run": {**body["run"], "tested_base": PR_HEAD}}):
            with self.subTest(invalid=invalid):
                census_file.write_text(json.dumps(invalid))
                code, err = self.command("workflow")
                self.assertEqual(1, code, err)
                self.assertIn("invalid prior census", err)
                self.assertFalse((self.census_dir / "ci-census").exists())
        census_file.write_text(json.dumps(body))
        code, err = self.command("workflow")
        self.assertEqual(0, code, err)
        self.assertEqual([f"ci-census-{CURRENT_JOB}-{ATTEMPT}.json"],
                         [path.name for path in (self.census_dir / "ci-census").iterdir()])

    def test_successful_job_rejects_missing_and_malformed_reports_before_writing_a_census(self):
        self.record_jobs("push", "success")
        report = self.emit_report("pass")
        path = self.report_dir / REPORT_FILE
        unit, = report["units"]
        invalid = (None, "{", json.dumps({**report, "job": "Another job"}),
                   json.dumps({**report, "units": []}), json.dumps({**report, "units": [unit, unit]}),
                   json.dumps({**report, "units": [{**unit, "collection_complete": "true"}]}),
                   json.dumps({**report, "units": [{**unit, "files": [{"path": "file.py"}]}]}))
        for content in invalid:
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_text(content)
                code, err = self.command("workflow", GITHUB_EVENT_NAME="push")
                self.assertEqual(1, code, err)
                self.assertIn("ci-units report", err)
                self.assertFalse((self.census_dir / "ci-census").exists())

    def test_failed_job_without_executor_evidence_records_a_failed_producer(self):
        self.record_jobs("push", "failure")
        code, err = self.command("workflow", GITHUB_EVENT_NAME="push")
        self.assertEqual(0, code, err)
        self.assertEqual([{"id": "job", "kind": "producer", "status": "fail", "reason": "test",
                           "collection_complete": True}], self.census()["units"])

    def test_failed_job_rejects_a_known_artifact_that_could_not_be_downloaded(self):
        self.artifacts = [CURRENT_ARTIFACT]
        self.record_jobs("push", "failure")
        self.assertEqual("suite-plan-artifact-id=109\n", (self.root / "step-output").read_text())
        code, err = self.command("workflow", GITHUB_EVENT_NAME="push", REPORT_DOWNLOAD_OUTCOME="failure")
        self.assertEqual(1, code, err)
        self.assertIn("ci-units artifact download failed", err)
        self.assertFalse((self.census_dir / "ci-census").exists())

    def test_pull_request_retains_base_when_a_failed_dependency_skips_the_report_job(self):
        toolkit = job(13, "Shared toolkit", "failure", **EXECUTION)
        self.record_jobs("pull_request", "skipped", other_jobs=[toolkit])
        code, err = self.command("workflow")
        self.assertEqual(0, code, err)
        census = self.census(slug="shared-toolkit")
        self.assertEqual(BASE, census["run"]["tested_base"])
        self.assertEqual("fail", census["units"][0]["status"])
        self.assertFalse((self.census_dir / "ci-census" / f"ci-census-{SLUG}-{ATTEMPT}.json").exists())

    def test_pull_request_rejects_a_merge_commit_that_does_not_match_the_tested_head(self):
        self.emit_report("pass")
        self.record_jobs("pull_request", "success")
        self.parents[1] = {"sha": "f" * 40}
        code, err = self.command("workflow")
        self.assertEqual(1, code, err)
        self.assertIn("does not identify the tested base", err)
        self.assertFalse((self.census_dir / "ci-census").exists())
