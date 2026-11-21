"""Stand-in for agents' scripts/companion_source.py with its consumer command contracts.

agents-revision prints revision=, source_ref=, companion=, and reason=; toolkit-paths prints one pathspec per line
from .github/toolkit-paths.json at the revision; stamp writes and prints each candidate-revision stamp.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMPS = {"default": (".candidate-revision",),
          "openclaw": (".candidate-revision", "packages/openclaw-shared/.candidate-revision")}


def git(probe, *arguments):
    return subprocess.run(["git", "-C", str(probe), *arguments], capture_output=True, text=True, check=True,
                          stdin=subprocess.DEVNULL, timeout=60).stdout


def require_partial(probe):
    if git(probe, "config", "--type=bool", "--default=false", "--get", "remote.origin.promisor").strip() != "true":
        raise SystemExit(f"{probe} is not a partial clone of agents")


def remote_ref(probe, ref):
    for line in git(probe, "ls-remote", "origin", ref).splitlines():
        commit, _, name = line.partition("\t")
        if name == ref:
            return commit
    raise SystemExit(f"agents origin has no {ref}")


def agents_revision(arguments):
    registry = json.loads((ROOT / "skills/repository/registry.json").read_text(encoding="utf-8"))
    agents = next(entry for entry in registry["repositories"] if entry["id"] == "agents")
    require_partial(arguments.probe)
    body = arguments.body_file.read_text(encoding="utf-8") if arguments.event == "pull_request" else ""
    match = re.search(rf"^Companion: {re.escape(agents['github'])}#(\d+)$", body, re.MULTILINE)
    ref = f"refs/pull/{match.group(1)}/head" if match else f"refs/heads/{agents['default_branch']}"
    print(f"revision={remote_ref(arguments.probe, ref)}")
    print(f"source_ref={ref}")
    print(f"companion={match.group(1) if match else ''}")
    print(f"reason={'companion declared' if match else 'no companion declared'}")


def toolkit_paths(arguments):
    require_partial(arguments.probe)
    git(arguments.probe, "fetch", "--quiet", "--depth=1", "--filter=blob:none", "origin", arguments.revision)
    document = json.loads(git(arguments.probe, "show", f"{arguments.revision}:.github/toolkit-paths.json"))
    print("\n".join(document["profiles"][arguments.profile]))


def stamp(arguments):
    for relative in STAMPS[arguments.profile]:
        path = arguments.root / relative
        path.write_text(f"{arguments.revision}\n", encoding="utf-8")
        print(path)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    revision = commands.add_parser("agents-revision")
    revision.add_argument("--probe", required=True, type=Path)
    revision.add_argument("--event", required=True)
    revision.add_argument("--body-file", required=True, type=Path)
    paths = commands.add_parser("toolkit-paths")
    paths.add_argument("--probe", required=True, type=Path)
    paths.add_argument("--revision", required=True)
    paths.add_argument("--profile", required=True)
    stamps = commands.add_parser("stamp")
    stamps.add_argument("--root", required=True, type=Path)
    stamps.add_argument("--revision", required=True)
    stamps.add_argument("--profile", required=True, choices=sorted(STAMPS))
    arguments = parser.parse_args()
    {"agents-revision": agents_revision, "toolkit-paths": toolkit_paths, "stamp": stamp}[arguments.command](arguments)


if __name__ == "__main__":
    sys.exit(main())
