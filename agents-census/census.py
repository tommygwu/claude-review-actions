"""Record every job of this run attempt and hand them to the agents census writer."""

import json
import os
import subprocess
import sys
from pathlib import Path

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


def jobs():
    run_id, attempt = int(environment("GITHUB_RUN_ID")), int(environment("GITHUB_RUN_ATTEMPT"))
    job_ids = json_input("INPUT_JOB_IDS", dict, "{}")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in job_ids.items()):
        raise CensusError("job-ids must map job display names to job ids")
    needs = json_input("INPUT_NEEDS", dict, "{}")
    api = Client(os.environ.get("GITHUB_API_URL", "https://api.github.com"), environment("GITHUB_REPOSITORY"),
                 os.environ.get("INPUT_GITHUB_TOKEN", ""))
    rows = api.pages(f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")[0]
    result = attempt_jobs(rows, run_id=run_id, attempt=attempt, head=head_sha(), needs=needs, job_ids=job_ids,
                          current_job=os.environ.get("INPUT_CURRENT_JOB", "") or "CI census",
                          report_job=environment("INPUT_REPORT_JOB"))
    census_dir().mkdir(parents=True, exist_ok=True)
    jobs_file().write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(result)} jobs of attempt {attempt}")


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
    command = [environment("CENSUS_PYTHON"), str(Path(environment("CENSUS_AGENTS")) / CENSUS_SCRIPT), "workflow",
               "--jobs-file", str(jobs_file()), "--report-dir", str(census_dir() / "reports"),
               "--report-job", report_job, "--current-job", current_job,
               "--output-dir", str(census_dir() / "ci-census")]
    for unit in units:
        command += ["--expected-unit", unit]
    result = subprocess.run(command, env=env, check=False)
    if result.returncode:
        raise CensusError(f"ci_census.py workflow exited {result.returncode}")


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
