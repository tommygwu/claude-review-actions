"""Record every job of this run attempt and hand them to the agents census writer."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from github_api import GitHub  # noqa: E402


CENSUS_SCRIPT = "packages/openclaw-shared/src/openclaw_shared/ci_census.py"


class CensusError(RuntimeError):
    pass


class Client(GitHub):
    label = "agents-census"


def environment(name):
    value = os.environ.get(name, "")
    if not value:
        raise CensusError(f"{name} is not set")
    return value


def census_dir():
    return Path(environment("CENSUS_DIR"))


def jobs_file():
    return census_dir() / "jobs.json"


def json_input(name, kind, default):
    raw = os.environ.get(name, "").strip() or default
    try:
        value = json.loads(raw)
    except ValueError:
        raise CensusError(f"{name.removeprefix('INPUT_').lower().replace('_', '-')} is not valid JSON") from None
    if not isinstance(value, kind):
        raise CensusError(f"{name.removeprefix('INPUT_').lower().replace('_', '-')} must be a JSON {kind.__name__}")
    return value


def head_sha():
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        event = json.loads(Path(environment("GITHUB_EVENT_PATH")).read_text(encoding="utf-8"))
        head = ((event.get("pull_request") or {}).get("head") or {}).get("sha")
        if not isinstance(head, str) or not head:
            raise CensusError("the pull_request event has no head SHA")
        return head
    return environment("GITHUB_SHA")


def matched_id(name, job_ids):
    for key, identifier in job_ids.items():
        if name == key or (name.startswith(f"{key} (") and name.endswith(")")):
            return identifier
    return None


def attempt_jobs(rows, *, run_id, attempt, head, needs, job_ids, current_job, report_job):
    if not any(job.get("name") == report_job for job in rows):
        raise CensusError(f"report-job {report_job!r} matches no job of this run; name the job's display name exactly")
    jobs = []
    for job in rows:
        job_attempt = job.get("run_attempt")
        if (type(job_attempt) is not int or not 1 <= job_attempt <= attempt
                or job.get("run_id") != run_id or job.get("head_sha") != head):
            raise CensusError("job identity differs from this run")
        if job_attempt != attempt:
            continue
        identifier = matched_id(job.get("name", ""), job_ids)
        if identifier is not None and job.get("conclusion") is None:
            need = needs.get(identifier)
            if not isinstance(need, dict) or not isinstance(need.get("result"), str):
                raise CensusError(f"job-ids maps {job.get('name')!r} to {identifier!r}, which needs does not list")
            job = {**job, "conclusion": need["result"]}
        jobs.append(job)
    if not any(job.get("name") == current_job for job in jobs):
        raise CensusError(f"current census job {current_job!r} is missing from this attempt")
    return jobs


def github():
    return Client(os.environ.get("GITHUB_API_URL", "https://api.github.com"), environment("GITHUB_REPOSITORY"),
                  os.environ.get("INPUT_GITHUB_TOKEN", ""))


def report_slug(job_name):
    return re.sub(r"[^a-z0-9]+", "-", job_name.lower()).strip("-")


def suite_plan_artifact_name(job_name, attempt):
    return f"ci-units-{report_slug(job_name)}-{attempt}"


def suite_plan_jobs(api, rows, current, *, run_id, attempt, head):
    current_names = {job["name"] for job in current}
    earlier = {job["name"]: job for job in sorted(rows, key=lambda job: job["run_attempt"])
               if job["name"] not in current_names}
    restored = current + list(earlier.values())
    execution = ("name", "status", "conclusion", "started_at", "completed_at", "steps")
    pending = {index: job for index, job in enumerate(current)
               if job.get("status") == "completed" and job.get("conclusion") == "success"
               and all(job.get(key) for key in execution)}

    # A failed-jobs rerun can list a copied successful execution under a new job ID.
    for older in range(1, attempt):
        if not pending:
            break
        previous, _ = api.pages(f"/actions/runs/{run_id}/attempts/{older}/jobs", "jobs")
        candidates = [job for job in previous if job.get("run_id") == run_id and job.get("head_sha") == head
                      and type(job.get("run_attempt")) is int and 1 <= job["run_attempt"] <= older]
        for index, job in list(pending.items()):
            matches = [row for row in candidates if all(row.get(key) == job[key] for key in execution)]
            if len(matches) > 1:
                raise CensusError(f"ambiguous carried execution for {job['name']!r}")
            if matches:
                restored[index] = matches[0]
                del pending[index]
    return restored


def suite_plan_downloads(api, run_id, artifact, carried):
    artifacts, _ = api.pages(f"/actions/runs/{run_id}/artifacts", "artifacts")
    active = [row for row in artifacts if not row.get("expired")]
    reports = [row["id"] for row in active if artifact and row.get("name") == artifact]
    if len(reports) > 1:
        raise CensusError(f"multiple artifacts named {artifact!r}")
    outputs = {"suite-plan-artifact-id": reports[0]} if reports else {}
    if carried:
        prior = [str(row["id"]) for row in active if str(row.get("name", "")).startswith("ci-census-")]
        if prior:
            outputs["prior-census-ids"] = ",".join(prior)
    return outputs


def jobs():
    run_id, attempt = int(environment("GITHUB_RUN_ID")), int(environment("GITHUB_RUN_ATTEMPT"))
    job_ids = json_input("INPUT_JOB_IDS", dict, "{}")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in job_ids.items()):
        raise CensusError("job-ids must map job display names to job ids")
    needs = json_input("INPUT_NEEDS", dict, "{}")
    api = github()
    rows, _ = api.pages(f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    head = head_sha()
    report_job = environment("INPUT_REPORT_JOB")
    result = attempt_jobs(rows, run_id=run_id, attempt=attempt, head=head, needs=needs, job_ids=job_ids,
                          current_job=os.environ.get("INPUT_CURRENT_JOB", "") or "CI census",
                          report_job=report_job)
    artifact = os.environ.get("INPUT_REPORT_ARTIFACT", "")
    outputs = {}
    if artifact.startswith("ci-units-"):
        result = suite_plan_jobs(api, rows, result, run_id=run_id, attempt=attempt, head=head)
        reports = [job for job in result if job["name"] == report_job]
        if len(reports) != 1:
            raise CensusError(f"expected one job named {report_job!r}")
        report = reports[0]
        original_artifact = suite_plan_artifact_name(report_job, report["run_attempt"])
        if artifact not in (suite_plan_artifact_name(report_job, attempt), original_artifact):
            raise CensusError(f"report-artifact must name the ci-units artifact for {report_job!r}")
        carried = any(job["run_attempt"] < attempt and job.get("conclusion") != "skipped" for job in result)
        artifact = original_artifact if report.get("conclusion") != "skipped" else None
        if artifact or carried:
            outputs = suite_plan_downloads(api, run_id, artifact, carried)
    elif artifact:
        outputs = {"legacy-report": "true"}

    census_dir().mkdir(parents=True, exist_ok=True)
    jobs_file().write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if outputs:
        with Path(environment("GITHUB_OUTPUT")).open("a", encoding="utf-8") as stream:
            for key, value in outputs.items():
                print(f"{key}={value}", file=stream)
    print(f"Recorded {len(result)} jobs for attempt {attempt}")


def tested_merge_base():
    merge = environment("GITHUB_SHA")
    commit = github().get(f"/commits/{merge}")
    parents = commit.get("parents") if isinstance(commit, dict) else None
    if (not isinstance(parents, list) or len(parents) != 2 or not all(isinstance(parent, dict) for parent in parents)
            or parents[1].get("sha") != head_sha() or not isinstance(base := parents[0].get("sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", base)):
        raise CensusError(f"pull request commit {merge} does not identify the tested base")
    return base


def report_path(value):
    return isinstance(value, str) and re.fullmatch(r"(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[^\\]+", value)


def matches_array(value, validator):
    return isinstance(value, list) and all(validator(item) for item in value)


def valid_case(case):
    return (isinstance(case, dict) and set(case) == {"id", "digest"}
            and isinstance(case["id"], str) and re.fullmatch(r"case-sha256:[0-9a-f]{16}", case["id"])
            and isinstance(case["digest"], str) and re.fullmatch(r"[0-9a-f]{64}", case["digest"]))


def valid_file(file):
    return (isinstance(file, dict) and file.keys() <= {"path", "status", "cases"}
            and report_path(file.get("path")) and file.get("status") in ("pass", "fail")
            and matches_array(file.get("cases", []), valid_case))


def valid_unit(unit):
    if (not isinstance(unit, dict) or not isinstance(unit.get("id"), str) or not unit["id"].strip()
            or unit.get("kind") not in ("test", "producer") or type(unit.get("collection_complete")) is not bool
            or not unit.keys() <= {"id", "kind", "status", "collection_complete", "reason", "covers", "files", "message", "runner"}
            or (unit["kind"] == "producer" and unit.keys() & {"covers", "files", "runner"})
            or ("runner" in unit and (not isinstance(unit["runner"], str)
                                     or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", unit["runner"])))
            or not matches_array(unit.get("covers", []), report_path)
            or not matches_array(unit.get("files", []), valid_file)
            or not isinstance(unit.get("message", ""), str) or len(unit.get("message", "")) > 500):
        return False
    covers = unit.get("covers", [])
    files = unit.get("files", [])
    if ("covers" in unit and not covers) or len(covers) != len(set(covers)):
        return False
    if len(files) != len({file["path"] for file in files}):
        return False
    reason = unit.get("reason")
    match unit.get("status"):
        case "pass":
            return "reason" not in unit and all(file["status"] == "pass" for file in files)
        case "fail":
            return reason in ("test", "timeout", "infrastructure")
        case "not-run":
            return (not files and isinstance(reason, str) and
                    re.fullmatch(r"(?:timeout|cancelled|infrastructure|skipped:[a-z0-9]+(?:-[a-z0-9]+)*)", reason))
        case _:
            return False


def valid_units(units):
    return bool(units) and matches_array(units, valid_unit) and len({unit["id"] for unit in units}) == len(units)


def suite_plan_units(directory, report):
    if report["conclusion"] == "skipped":
        return []
    files = list(directory.rglob("ci-units-*.json"))
    outcome = os.environ.get("REPORT_DOWNLOAD_OUTCOME")
    if outcome == "failure":
        raise CensusError("the ci-units artifact download failed")
    if not files and outcome != "success" and report["conclusion"] in ("failure", "timed_out", "cancelled"):
        return []
    if len(files) != 1 or files[0].name != f"ci-units-{report_slug(report['name'])}.json":
        raise CensusError(f"expected one ci-units report from {report['name']!r} attempt {report['run_attempt']}")
    try:
        body = json.loads(files[0].read_text(encoding="utf-8"))
    except ValueError:
        raise CensusError(f"malformed ci-units report: {files[0].name}") from None
    units = body.get("units") if isinstance(body, dict) else None
    if not valid_units(units) or body.get("job") != report["name"]:
        raise CensusError(f"malformed ci-units report: {files[0].name}")
    return units


def write_census(job_path, report_dir, report_job, current_job, units, env):
    command = [environment("CENSUS_PYTHON"), str(Path(environment("CENSUS_AGENTS")) / CENSUS_SCRIPT), "workflow",
               "--jobs-file", str(job_path), "--report-dir", str(report_dir),
               "--report-job", report_job, "--current-job", current_job,
               "--output-dir", str(census_dir() / "ci-census")]
    for unit in units:
        command += ["--expected-unit", unit]
    result = subprocess.run(command, env=env, check=False)
    if result.returncode:
        raise CensusError(f"ci_census.py workflow exited {result.returncode}")


def census_name(job):
    return f"ci-census-{report_slug(job['name'])}-{job['run_attempt']}.json"


def prior_census(job, env, *, report):
    directory = census_dir() / "prior-censuses"
    files = list(directory.rglob(census_name(job)))
    if not files:
        return False
    if len(files) != 1:
        raise CensusError(f"multiple prior census files for {job['name']!r} attempt {job['run_attempt']}")
    body = json.loads(files[0].read_text(encoding="utf-8"))
    run = body.get("run") if isinstance(body, dict) else None
    identity = {"repository": env["GITHUB_REPOSITORY"], "run_id": int(env["GITHUB_RUN_ID"]),
                "run_attempt": job["run_attempt"], "head_sha": env["GITHUB_SHA"], "job": job["name"],
                "workflow": env["GITHUB_WORKFLOW"], "event": env["GITHUB_EVENT_NAME"], "source": "ci"}
    if base := env.get("OPENCLAW_CI_TESTED_BASE"):
        identity["tested_base"] = base
    if (not isinstance(run, dict) or any(run.get(key) != value for key, value in identity.items())
            or not all(valid_timestamp(run.get(key)) for key in ("started_at", "finished_at"))
            or body.get("schema") != "ci-census" or type(body.get("truncated")) is not bool
            or not valid_units(body.get("units"))):
        raise CensusError(f"invalid prior census: {files[0].name}")
    producer_pass = {"id": "job", "kind": "producer", "status": "pass", "collection_complete": True}
    if job is report and job["conclusion"] == "success" and body["units"] == [producer_pass]:
        if suite_plan_units(census_dir() / "reports", job) != body["units"]:
            raise CensusError(f"prior census has only the producer result for {job['name']!r}")
    return True


def valid_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def census_time(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def restore_execution_provenance(jobs, report, units, expected, tested_base):
    for job in jobs:
        if job["conclusion"] == "skipped":
            continue
        path = census_dir() / "ci-census" / census_name(job)
        census = json.loads(path.read_text(encoding="utf-8"))
        run = census["run"]
        for field, source in (("started_at", "started_at"), ("finished_at", "completed_at")):
            if value := job.get(source):
                run[field] = census_time(value)
        if tested_base:
            run["tested_base"] = tested_base
        if job is report:
            # The adapter kept every row. Incomplete or skipped units carry their own status.
            census["truncated"] = bool(expected) and {unit["id"] for unit in units} != set(expected)
        path.write_text(json.dumps(census, sort_keys=True) + "\n", encoding="utf-8")


def write_suite_plan_census(recorded, report, current_job, expected, env):
    if report is None:
        raise CensusError("ci-units report job is missing from the recorded jobs")
    if env["GITHUB_EVENT_NAME"] == "pull_request" and "OPENCLAW_CI_TESTED_BASE" not in env:
        env["OPENCLAW_CI_TESTED_BASE"] = tested_merge_base()

    report_job = report["name"]
    attempt = int(env["GITHUB_RUN_ATTEMPT"])
    recorded = [job for job in recorded if job["run_attempt"] == attempt or job["conclusion"] == "skipped"
                or not prior_census(job, env, report=report)]
    units = suite_plan_units(census_dir() / "reports", report) if any(job is report for job in recorded) else []

    with TemporaryDirectory(dir=census_dir()) as tmp:
        directory = Path(tmp)
        job_path = directory / "jobs.json"
        job_path.write_text(json.dumps(recorded), encoding="utf-8")
        report_dir = directory / "reports"
        report_dir.mkdir()

        # Use the writer's invocation-scoped report envelope and preserve each unit intact.
        for index, unit in enumerate(units):
            body = {"run_id": f"{env['GITHUB_RUN_ID']}.{report['run_attempt']}", "head_sha": env["GITHUB_SHA"],
                    "unit": unit}
            (report_dir / f"node-census-suite-plan-{index:08d}.json").write_text(json.dumps(body), encoding="utf-8")
        write_census(job_path, report_dir, report_job, current_job, expected, env)
    restore_execution_provenance(recorded, report, units, expected, env.get("OPENCLAW_CI_TESTED_BASE"))


def workflow():
    recorded = json.loads(jobs_file().read_text(encoding="utf-8"))
    report_job = environment("INPUT_REPORT_JOB")
    current_job = os.environ.get("INPUT_CURRENT_JOB", "") or "CI census"
    selected = bool(os.environ.get("INPUT_EXPECTED_UNITS", "").strip())
    units = json_input("INPUT_EXPECTED_UNITS", list, "[]")
    if not all(isinstance(unit, str) and unit for unit in units):
        raise CensusError("expected-units must be a JSON array of unit names")
    require = (os.environ.get("INPUT_REQUIRE_SELECTION", "") or "false").strip().lower()
    if require not in ("true", "false"):
        raise CensusError(f"require-selection must be true or false, not {require!r}")
    report = next((job for job in recorded if job.get("name") == report_job), None)
    if (require == "true" and os.environ.get("GITHUB_EVENT_NAME") == "pull_request" and not selected
            and report and report.get("conclusion") == "success"):
        raise CensusError(f"successful {report_job} has no census selection")
    env = os.environ.copy()
    env.pop("OPENCLAW_CI_TESTED_BASE", None)
    tested_base = os.environ.get("INPUT_TESTED_BASE", "").strip()
    if tested_base and report and report.get("conclusion") != "skipped":
        env["OPENCLAW_CI_TESTED_BASE"] = tested_base
    if os.environ.get("INPUT_REPORT_ARTIFACT", "").startswith("ci-units-"):
        write_suite_plan_census(recorded, report, current_job, units, env)
    else:
        write_census(jobs_file(), census_dir() / "reports", report_job, current_job, units, env)


COMMANDS = {"jobs": jobs, "workflow": workflow}


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 1 or arguments[0] not in COMMANDS:
        print(f"usage: census.py {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    try:
        COMMANDS[arguments[0]]()
    except (CensusError, ValueError, OSError) as error:
        print(f"::error title=agents-census::{' '.join(str(error).split())}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
