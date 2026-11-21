"""Resolve, archive, and clean up the agents toolkit for one CI run."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from github_api import GitHub  # noqa: E402


COMMIT = re.compile(r"[0-9a-f]{40}\Z")
MINIMUM_GIT = (2, 44)
RESOLVER = "scripts/companion_source.py"
RESOLVER_TIMEOUT_SECONDS = 600
GIT_TIMEOUT_SECONDS = 120
HYDRATE_TIMEOUT_SECONDS = 180
GITLINK_MODE = "160000"


class FetchError(RuntimeError):
    pass


class Client(GitHub):
    label = "agents-toolkit-fetch"


def environment(name):
    value = os.environ.get(name, "")
    if not value:
        raise FetchError(f"{name} is not set")
    return value


def runner_temp():
    return Path(environment("RUNNER_TEMP")).resolve()


def workspace_probe():
    return Path(environment("GITHUB_WORKSPACE")) / ".agents-toolkit-probe"


def probe():
    return runner_temp() / "agents-probe"


def archive_path():
    return runner_temp() / "agents-toolkit.tar"


def client():
    return Client(os.environ.get("GITHUB_API_URL", "https://api.github.com"), environment("GITHUB_REPOSITORY"),
                  os.environ.get("FETCH_GITHUB_TOKEN", ""))


def git_environment(*, lazy=True):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if not lazy:
        env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def git(arguments, *, cwd, lazy=True, stdin=None, timeout=GIT_TIMEOUT_SECONDS, binary=False):
    try:
        result = subprocess.run(["git", "--literal-pathspecs", *arguments], cwd=cwd, env=git_environment(lazy=lazy),
                                input=stdin, capture_output=True, timeout=timeout, check=False, text=not binary)
    except subprocess.TimeoutExpired:
        raise FetchError(f"git {arguments[0]} did not finish in {timeout} seconds") from None
    if result.returncode:
        detail = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
        raise FetchError(f"git {arguments[0]} exited {result.returncode}: {' '.join(detail.split()) or '<no stderr>'}")
    return result.stdout


def write_output(name, value):
    with open(environment("GITHUB_OUTPUT"), "a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def git_version():
    output = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False).stdout
    match = re.search(r"(\d+)\.(\d+)", output)
    if not match:
        raise FetchError(f"cannot read the git version from {output.strip()!r}")
    return int(match.group(1)), int(match.group(2))


def repository_private():
    value = os.environ.get("FETCH_REPOSITORY_PRIVATE", "").strip().lower()
    if value in ("true", "false"):
        return value == "true"
    record = client().get("")
    if not isinstance(record, dict) or type(record.get("private")) is not bool:
        raise FetchError("GitHub returned no repository visibility")
    return record["private"]


def check():
    if not os.environ.get("FETCH_DEPLOY_KEY", "").strip():
        raise FetchError("deploy-key is empty; fork pull requests receive no secrets, "
                         "so run the toolkit fetch only for same-repository pull requests")
    version = git_version()
    if version < MINIMUM_GIT:
        raise FetchError(f"fetch needs git >= {MINIMUM_GIT[0]}.{MINIMUM_GIT[1]} (GIT_NO_LAZY_FETCH), "
                         f"found {version[0]}.{version[1]}")
    upload = os.environ.get("FETCH_UPLOAD", "true").strip().lower()
    if upload not in ("true", "false"):
        raise FetchError(f"upload must be true or false, not {upload!r}")
    if upload == "true" and not repository_private():
        raise FetchError("upload is true in a public repository, whose artifacts any signed-in GitHub user can "
                         "download; set upload: false and restore in the same job")


def move():
    source, target = workspace_probe(), probe()
    if not (source / ".git").is_dir():
        raise FetchError(f"the agents probe checkout is missing at {source}")
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(source), str(target))


def resolver(arguments, *, timeout):
    command = [sys.executable, str(probe() / RESOLVER), *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False,
                                env=git_environment())
    except subprocess.TimeoutExpired:
        raise FetchError(f"{arguments[0]} did not finish in {timeout} seconds") from None
    sys.stderr.write(result.stderr)
    if result.returncode:
        raise FetchError(f"{arguments[0]} exited {result.returncode}")
    return result.stdout


def pull_request_body():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return ""
    event = json.loads(Path(environment("GITHUB_EVENT_PATH")).read_text(encoding="utf-8"))
    number = (event.get("pull_request") or {}).get("number")
    if type(number) is not int or number <= 0:
        raise FetchError("the pull_request event has no pull request number")
    record = client().get(f"/pulls/{number}")
    if not isinstance(record, dict) or "body" not in record:
        raise FetchError(f"GitHub returned no body for pull request #{number}")
    body = record["body"]
    if body is not None and not isinstance(body, str):
        raise FetchError(f"GitHub returned an invalid body for pull request #{number}")
    return body or ""


def resolve():
    body_file = runner_temp() / "agents-toolkit-pr-body.txt"
    body_file.write_text(pull_request_body(), encoding="utf-8")
    output = resolver(["agents-revision", "--probe", str(probe()), "--event", os.environ.get("GITHUB_EVENT_NAME", ""),
                       "--body-file", str(body_file)], timeout=RESOLVER_TIMEOUT_SECONDS)
    sys.stdout.write(output)
    revisions = [line.removeprefix("revision=") for line in output.splitlines() if line.startswith("revision=")]
    if len(revisions) != 1 or not COMMIT.fullmatch(revisions[0]):
        raise FetchError("agents-revision did not print exactly one revision=<40-hex commit> line")
    write_output("revision", revisions[0])
    return revisions[0]


def tree_entries(revision, paths):
    arguments = ["ls-tree", "-r", "-z", "--full-name", revision, "--", *paths]
    entries = []
    for record in git(arguments, cwd=probe(), lazy=False).split("\0"):
        if record:
            meta, _, path = record.partition("\t")
            mode, kind, oid = meta.split()
            entries.append((mode, kind, oid, path))
    return entries


def missing_objects(revision):
    listing = git(["rev-list", "--objects", "--missing=print", revision], cwd=probe(), lazy=False)
    return sorted({line[1:].split()[0] for line in listing.splitlines() if line.startswith("?")})


def hydrate(revision):
    """Fetch every missing blob of the revision in one request.

    `git archive` reads every blob of the tree, not only those its pathspec selects, so a partial hydration
    would leave it fetching the rest itself.
    """
    missing = missing_objects(revision)
    if missing:
        git(["-c", "fetch.negotiationAlgorithm=noop", "fetch", "--no-tags", "--no-write-fetch-head",
             "--recurse-submodules=no", "--filter=blob:none", "origin", "--stdin"],
            cwd=probe(), stdin="".join(f"{oid}\n" for oid in missing), timeout=HYDRATE_TIMEOUT_SECONDS)
        remaining = missing_objects(revision)
        if remaining:
            raise FetchError(f"{len(remaining)} toolkit objects are still missing after hydration, "
                             f"for example {remaining[0]}")
    print(f"Hydrated {len(missing)} missing objects in one fetch")


def member_parts(name):
    return [part for part in name.split("/") if part not in ("", ".")]


def verify_archive(tar_path, revision, expected, deploy_key):
    with tar_path.open("rb") as stream:
        commit = subprocess.run(["git", "get-tar-commit-id"], stdin=stream, capture_output=True,
                                text=True, check=False).stdout.strip()
    if commit != revision:
        raise FetchError(f"the archive's embedded commit is {commit or '<none>'}, not {revision}")
    key = deploy_key.strip().encode()
    found = set()
    with tarfile.open(tar_path) as archive:
        for member in archive:
            parts = member_parts(member.name)
            if member.name.startswith("/") or ".." in parts:
                raise FetchError("an archive member leaves the archive root")
            if ".git" in parts:
                raise FetchError("the archive holds git metadata")
            if member.isfile() or member.issym():
                found.add("/".join(parts))
            if key and member.isfile() and key in archive.extractfile(member).read():
                raise FetchError("the archive holds the deploy key")
    absent, extra = sorted(expected - found), sorted(found - expected)
    if absent or extra:
        sample = (absent or extra)[0]
        raise FetchError(f"the archive differs from its profile: {len(absent)} files missing, {len(extra)} extra, "
                         f"for example {sample}")
    return len(found)


def archive():
    revision = environment("FETCH_REVISION")
    profile = os.environ.get("FETCH_PROFILE", "") or "default"
    if not COMMIT.fullmatch(revision):
        raise FetchError(f"invalid toolkit revision: {revision}")
    paths = [line for line in resolver(["toolkit-paths", "--probe", str(probe()), "--revision", revision,
                                        "--profile", profile], timeout=GIT_TIMEOUT_SECONDS).splitlines() if line]
    if not paths:
        raise FetchError(f"toolkit-paths listed no paths for profile {profile}")
    hydrate(revision)
    tar_path = archive_path()
    git(["archive", "--format=tar", "-o", str(tar_path), revision, "--", *paths], cwd=probe(), lazy=False)
    staging = Path(tempfile.mkdtemp(prefix="agents-toolkit-stamps-", dir=runner_temp()))
    try:
        for directory in git(["ls-tree", "-r", "-d", "-z", "--name-only", "--full-name", revision, "--", *paths],
                             cwd=probe(), lazy=False).split("\0"):
            if directory:
                (staging / directory).mkdir(parents=True, exist_ok=True)
        stamped = [Path(line) for line in resolver(["stamp", "--root", str(staging), "--revision", revision,
                                                    "--profile", profile], timeout=GIT_TIMEOUT_SECONDS).splitlines()
                   if line]
        names = []
        with tarfile.open(tar_path, "a") as tar:
            for path in stamped:
                name = path.resolve().relative_to(staging.resolve()).as_posix()
                tar.add(path, arcname=name, recursive=False)
                names.append(name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    expected = {path for mode, kind, _, path in tree_entries(revision, paths) if mode != GITLINK_MODE and kind == "blob"}
    count = verify_archive(tar_path, revision, expected | set(names), os.environ.get("FETCH_DEPLOY_KEY", ""))
    print(f"Archived {count} toolkit files for profile {profile} at {revision}")


def credential_paths(checkout):
    config = checkout / ".git" / "config"
    if not config.is_file():
        return []
    paths = []
    command = subprocess.run(["git", "config", "--file", str(config), "--get", "core.sshCommand"],
                             capture_output=True, text=True, check=False).stdout.strip()
    tokens = shlex.split(command) if command else []
    for index, token in enumerate(tokens):
        if token == "-i" and index + 1 < len(tokens):
            paths.append(tokens[index + 1])
        elif token.startswith("UserKnownHostsFile="):
            paths.append(token.split("=", 1)[1])
    includes = subprocess.run(["git", "config", "--file", str(config), "--null", "--get-regexp",
                               r"^includeif\..*\.path$"], capture_output=True, text=True, check=False).stdout
    paths += [record.split("\n", 1)[1] for record in includes.split("\0") if "\n" in record]
    return paths


def key_copies(temp, key):
    if not key:
        return []
    copies = []
    for path in temp.iterdir():
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536:
            if path.read_bytes().strip() == key:
                copies.append(path)
    return copies


def cleanup():
    temp = runner_temp()
    for checkout in (workspace_probe(), probe()):
        for raw in credential_paths(checkout):
            path = Path(os.path.expandvars(raw)).resolve()
            if path.is_relative_to(temp) and path != temp and path.is_file():
                path.unlink()
        if checkout.exists():
            shutil.rmtree(checkout)
    key = os.environ.get("FETCH_DEPLOY_KEY", "").strip().encode()
    for path in key_copies(temp, key):
        path.unlink()
    left = [str(checkout) for checkout in (workspace_probe(), probe()) if checkout.exists()]
    left += [str(path) for path in key_copies(temp, key)]
    if left:
        raise FetchError(f"cleanup left {len(left)} probe or deploy-key copies")
    print("Removed the agents probe and every deploy-key copy")


COMMANDS = {"check": check, "move": move, "resolve": resolve, "archive": archive, "cleanup": cleanup}


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 1 or arguments[0] not in COMMANDS:
        print(f"usage: fetch.py {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    try:
        COMMANDS[arguments[0]]()
    except (FetchError, ValueError, OSError, tarfile.TarError) as error:
        print(f"::error title=agents-toolkit-fetch::{' '.join(str(error).split())}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
