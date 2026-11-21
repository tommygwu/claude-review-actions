"""Unpack the agents toolkit and provision its own runtimes without changing the job's PATH."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen


COMMIT = re.compile(r"[0-9a-f]{40}\n?\Z")
SERIES = re.compile(r"[0-9]+\.[0-9]+\Z")
PATCH = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RUNTIME_VERSIONS = ".github/actions/mise-python/runtime-versions.sh"
PIN_FILES = (".node-version", ".python-version", "package.json", RUNTIME_VERSIONS)
MISE_RELEASES = "https://github.com/jdx/mise/releases/download"
DOWNLOAD_TIMEOUT_SECONDS = 120
MOUNTINFO = Path("/proc/self/mountinfo")
USERS = Path("/Users")
HOST_ROOT = Path("/Users/Shared/agents")


class RestoreError(RuntimeError):
    pass


def environment(name):
    value = os.environ.get(name, "")
    if not value:
        raise RestoreError(f"{name} is not set")
    return value


def runner_temp():
    return Path(environment("RUNNER_TEMP"))


def toolkit():
    return runner_temp() / "agents-toolkit"


def archive_path():
    return runner_temp() / "agents-toolkit.tar"


def mise_path():
    return runner_temp() / "mise" / "bin" / "mise"


def append(variable, lines):
    with open(environment(variable), "a", encoding="utf-8") as stream:
        for name, value in lines:
            if "\n" in str(value):
                raise RestoreError(f"{name} holds a newline")
            stream.write(f"{name}={value}\n")


def flag(name, default):
    value = (os.environ.get(name, "") or default).strip().lower()
    if value not in ("true", "false"):
        raise RestoreError(f"{name.removeprefix('INPUT_').lower()} must be true or false, not {value!r}")
    return value == "true"


def github_hosted():
    return os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"


def unpack():
    tar_path, target = archive_path(), toolkit()
    if not tar_path.is_file():
        if os.environ.get("DOWNLOAD_OUTCOME") == "failure":
            raise RestoreError("the agents-toolkit download step failed; see its log, then re-run all jobs if the "
                               "artifact is missing")
        raise RestoreError("artifact agents-toolkit is missing; re-run all jobs so the fetch job uploads it")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    result = subprocess.run(["tar", "-xf", str(tar_path), "-C", str(target)], capture_output=True, text=True,
                            check=False)
    if result.returncode:
        raise RestoreError(f"tar could not unpack the toolkit: {' '.join(result.stderr.split())}")
    stamp = target / ".candidate-revision"
    content = stamp.read_text(encoding="utf-8") if stamp.is_file() else ""
    if not COMMIT.fullmatch(content):
        raise RestoreError("the toolkit has no .candidate-revision holding one 40-hex commit")
    print(f"Agents toolkit revision: {content.strip()}")


def pin_digest(root):
    digest = hashlib.sha256()
    for name in PIN_FILES:
        path = root / name
        if not path.is_file():
            raise RestoreError(f"the toolkit has no {name}")
        content = path.read_bytes()
        if name == "package.json":
            try:
                document = json.loads(content)
            except ValueError:
                raise RestoreError("the toolkit package.json is not valid JSON") from None
            manager = document.get("packageManager") if isinstance(document, dict) else None
            if not isinstance(manager, str) or not manager:
                raise RestoreError("the toolkit package.json has no packageManager")
            content = manager.encode()
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()[:24]


def plan():
    cache, save = flag("INPUT_CACHE", "true"), flag("INPUT_SAVE", "false")
    prefix = (f"agents-toolkit-{environment('RUNNER_OS')}-{environment('RUNNER_ARCH')}-"
              f"{pin_digest(toolkit())}-")
    restore = cache and github_hosted()
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    trusted = event == "schedule" or (
        event == "push" and os.environ.get("DEFAULT_BRANCH")
        and os.environ.get("GITHUB_REF") == f"refs/heads/{os.environ['DEFAULT_BRANCH']}")
    append("GITHUB_OUTPUT", [("prefix", prefix), ("restore", str(restore).lower()),
                             ("save", str(bool(restore and save and trusted)).lower())])
    print(f"Runtime cache: prefix {prefix}, restore {str(restore).lower()}, "
          f"save {str(bool(restore and save and trusted)).lower()}")


def mise_release():
    script = toolkit() / RUNTIME_VERSIONS
    result = subprocess.run(
        ["bash", "-c", 'set -e; source "$1"; printf "%s\\n%s\\n%s\\n" "$CI_MISE_VERSION" "$CI_MISE_ASSET" '
                       '"$CI_MISE_SHA256"', "runtime-versions", str(script)],
        capture_output=True, text=True, check=False)
    values = result.stdout.splitlines()
    if result.returncode or len(values) != 3 or not all(values):
        detail = " ".join(result.stderr.split())
        raise RestoreError(f"{RUNTIME_VERSIONS} lists no mise download for this platform"
                           + (f": {detail}" if detail else ""))
    version, asset, sha256 = values
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(r"[a-z0-9-]+", asset) \
            or not SHA256.fullmatch(sha256):
        raise RestoreError(f"{RUNTIME_VERSIONS} lists an invalid mise download")
    return version, asset, sha256


def download(url):
    try:
        with urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            return response.read()
    except OSError as error:
        raise RestoreError(f"cannot download {url}: {error}") from None


def mise():
    version, asset, sha256 = mise_release()
    url = f"{MISE_RELEASES}/v{version}/mise-v{version}-{asset}"
    content = download(url)
    if hashlib.sha256(content).hexdigest() != sha256:
        raise RestoreError(f"mise {version} {asset} does not match the checksum in {RUNTIME_VERSIONS}")
    target = mise_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        stream.write(content)
    os.chmod(stream.name, 0o755)
    os.replace(stream.name, target)
    print(f"Installed mise {version} ({asset}) at {target}")


def run_mise(arguments, *, offline=False):
    env = os.environ.copy()
    env["MISE_CEILING_PATHS"] = str(runner_temp().parent)
    if offline:
        env["MISE_OFFLINE"] = "1"
    result = subprocess.run([str(mise_path()), *arguments], cwd=runner_temp(), env=env, capture_output=True,
                            text=True, check=False)
    if result.returncode:
        raise RestoreError(f"mise {' '.join(arguments)} exited {result.returncode}: "
                           f"{' '.join(result.stderr.split()) or '<no stderr>'}")
    return result.stdout


def series():
    value = (toolkit() / ".python-version").read_text(encoding="utf-8").strip()
    if not SERIES.fullmatch(value):
        raise RestoreError(f"the toolkit .python-version must hold a MAJOR.MINOR series, not {value!r}")
    return value


def installed_patches(pin):
    output = run_mise(["ls", "--installed", "python", "--json"], offline=True).strip() or "[]"
    try:
        records = json.loads(output)
    except ValueError:
        raise RestoreError("mise ls --installed python --json did not print JSON") from None
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise RestoreError("mise ls --installed python --json did not print a list of installs")
    versions = [record.get("version") for record in records]
    return sorted((version for version in versions
                   if isinstance(version, str) and PATCH.fullmatch(version) and version.startswith(f"{pin}.")),
                  key=lambda version: tuple(int(part) for part in version.split(".")))


def python():
    pin = series()
    saving = flag("CACHE_SAVE", "false")
    patches = installed_patches(pin)
    if not patches or saving or not github_hosted():
        run_mise(["install", f"python@{pin}"])
        patches = installed_patches(pin)
    else:
        print(f"Using installed Python {patches[-1]} (offline)")
    if not patches:
        raise RestoreError(f"no CPython {pin} patch is installed")
    patch = patches[-1]
    if saving:
        for older in patches[:-1]:
            run_mise(["uninstall", f"python@{older}"])
    interpreter = Path(run_mise(["where", f"python@{patch}"], offline=True).strip()) / "bin" / f"python{pin}"
    if not os.access(interpreter, os.X_OK):
        raise RestoreError(f"{interpreter} is not executable")
    append("GITHUB_OUTPUT", [("patch", patch)])
    print(f"Toolkit Python: {patch}")


def export():
    pin = (toolkit() / ".node-version").read_text(encoding="utf-8").strip().removeprefix("v")
    node = Path(run_mise(["where", f"node@{pin}"], offline=True).strip()) / "bin" / "node"
    if not os.access(node, os.X_OK):
        raise RestoreError(f"toolkit Node {pin} is not executable at {node}")
    append("GITHUB_ENV", [("REPO_RUNTIME_MISE_BIN", mise_path()), ("AGENTS_REPO", toolkit()),
                          ("AGENTS_NODE", node), ("AGENTS_ARCHIVE", archive_path())])
    print(f"Exported AGENTS_REPO={toolkit()} AGENTS_NODE={node} AGENTS_ARCHIVE={archive_path()}")


def host_mount_reachable():
    mountinfo, users = MOUNTINFO, USERS
    if not os.access(mountinfo, os.R_OK) or users.is_symlink() or (users / "Shared").is_symlink():
        return True
    for line in mountinfo.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) > 4 and (fields[4] == str(users) or fields[4].startswith(f"{users}/")):
            return True
    return False


def host_link():
    subpath = os.environ.get("HOST_LINK", "").strip()
    parts = Path(subpath).parts
    if not subpath or Path(subpath).is_absolute() or ".." in parts:
        raise RestoreError(f"host-link must be a relative toolkit subpath without '..', not {subpath!r}")
    if not github_hosted() and host_mount_reachable():
        raise RestoreError("the runner can reach a host /Users mount; refusing to link /Users/Shared/agents")
    source = (toolkit() / subpath).resolve()
    if not source.is_relative_to(toolkit().resolve()) or not source.exists():
        raise RestoreError(f"the toolkit has no {subpath}")
    target = HOST_ROOT if subpath in (".", "./") else HOST_ROOT / subpath
    if target.exists() and not target.is_symlink():
        raise RestoreError(f"{target} exists and is not a link; refusing to replace it")
    for command in (["sudo", "mkdir", "-p", str(target.parent)], ["sudo", "ln", "-sfn", str(source), str(target)]):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RestoreError(f"{' '.join(command[:2])} failed: {' '.join(result.stderr.split())}")
    if Path(os.path.realpath(target)) != source:
        raise RestoreError(f"{target} does not resolve to {source}")
    print(f"Linked {target} to {source}")


COMMANDS = {"unpack": unpack, "plan": plan, "mise": mise, "python": python, "export": export,
            "host-link": host_link}


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 1 or arguments[0] not in COMMANDS:
        print(f"usage: restore.py {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    try:
        COMMANDS[arguments[0]]()
    except (RestoreError, OSError) as error:
        print(f"::error title=agents-toolkit::{' '.join(str(error).split())}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
