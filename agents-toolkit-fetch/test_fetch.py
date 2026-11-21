"""Fetch against a local bare agents repository over file://, cloned the way actions/checkout clones the probe."""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import fetch

HERE = Path(__file__).resolve().parent
REPOSITORY = "tommygwu/consumer"
API = "https://api.github.com"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-deploy-key\n-----END OPENSSH PRIVATE KEY-----"
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Fixture",
           "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "Fixture",
           "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
MAIN_FILES = {
    ".gitattributes": "*.sh text eol=lf\n",
    ".node-version": "26.1.0\n",
    ".github/toolkit-paths.json": json.dumps({"profiles": {"default": [".node-version", "skills/repo-runtime"],
                                                           "openclaw": ["."]}}),
    "scripts/companion_source.py": (HERE / "fixtures" / "companion_source.py").read_text(encoding="utf-8"),
    "skills/repository/registry.json": json.dumps({"repositories": [
        {"id": "agents", "github": "tommygwu/agents", "default_branch": "main"}]}),
    "skills/repo-runtime/scripts/repo-runtime": "#!/bin/sh\nexit 0\n",
    "skills/repo-runtime/notes.txt": "repo-runtime notes\n",
    "packages/openclaw-shared/src/openclaw_shared/__init__.py": "",
}
SYMLINK = "docs/node-version"
OPENCLAW_MEMBERS = set(MAIN_FILES) | {SYMLINK, ".candidate-revision", "packages/openclaw-shared/.candidate-revision"}
DEFAULT_MEMBERS = {".node-version", "skills/repo-runtime/scripts/repo-runtime", "skills/repo-runtime/notes.txt",
                   ".candidate-revision"}
COMPANION_MEMBERS = {".node-version", "skills/repo-runtime/scripts/repo-runtime", "skills/repo-runtime/notes.txt",
                     "docs/companion.md", ".candidate-revision"}


def git(*arguments, cwd=None):
    return subprocess.run(["git", *arguments], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True,
                          text=True, check=True).stdout.strip()


class Response:
    def __init__(self, document):
        self.body = json.dumps(document).encode()
        self.headers = {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def offline(request, timeout):
    raise AssertionError(f"unexpected GitHub request {request.full_url}")


class FetchFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        source, origin = Path(cls.template.name) / "source", Path(cls.template.name) / "agents.git"
        git("init", "-q", "-b", "main", str(source))
        for name, content in MAIN_FILES.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(content, encoding="utf-8")
        (source / "skills/repo-runtime/scripts/repo-runtime").chmod(0o755)
        (source / "docs").mkdir()
        (source / SYMLINK).symlink_to("../.node-version")
        git("add", "-A", cwd=source)
        git("commit", "-q", "-m", "main", cwd=source)
        git("checkout", "-q", "-b", "companion", cwd=source)
        (source / "docs/companion.md").write_text("companion\n", encoding="utf-8")
        (source / ".github/toolkit-paths.json").write_text(json.dumps({"profiles": {
            "default": [".node-version", "skills/repo-runtime", "docs/companion.md"], "openclaw": ["."]}}),
            encoding="utf-8")
        git("add", "-A", cwd=source)
        git("commit", "-q", "-m", "companion", cwd=source)
        git("init", "-q", "--bare", str(origin))
        git("config", "uploadpack.allowFilter", "true", cwd=origin)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=origin)
        git("push", "-q", str(origin), "main", "companion:refs/pull/7/head", cwd=source)
        cls.main = git("rev-parse", "main", cwd=source)
        cls.companion = git("rev-parse", "companion", cwd=source)

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.temp, self.workspace = self.root / "runner-temp", self.root / "work space"
        self.temp.mkdir()
        self.workspace.mkdir()
        self.origin = self.root / "agents.git"
        shutil.copytree(Path(self.template.name) / "agents.git", self.origin)
        self.event = self.root / "event.json"
        self.event.write_text("{}", encoding="utf-8")
        self.output = self.root / "github-output"
        environment = patch.dict(os.environ, {
            **GIT_ENV, "RUNNER_TEMP": str(self.temp), "GITHUB_WORKSPACE": str(self.workspace),
            "GITHUB_OUTPUT": str(self.output), "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(self.event),
            "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_API_URL": API, "FETCH_GITHUB_TOKEN": "job-token",
            "FETCH_DEPLOY_KEY": KEY, "FETCH_UPLOAD": "false", "FETCH_REPOSITORY_PRIVATE": "", "FETCH_PROFILE": "",
            "FETCH_RESOLVER_PYTHON": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.requests = []
        self.api = offline
        transport = patch.object(fetch.Client, "transport", lambda client, request, timeout: self.api(request, timeout))
        transport.start()
        self.addCleanup(transport.stop)

    def checkout_probe(self):
        """Clone the probe as actions/checkout does: shallow, blob:none, sparse, with the persisted SSH key."""
        probe = self.workspace / ".agents-toolkit-probe"
        git("clone", "-q", "--filter=blob:none", "--depth=1", "--no-checkout", "--branch", "main",
            self.origin.as_uri(), str(probe))
        git("sparse-checkout", "set", "--no-cone", "scripts/companion_source.py", "skills/repository/registry.json",
            cwd=probe)
        git("checkout", "-q", "main", cwd=probe)
        self.assertEqual("true", git("config", "remote.origin.promisor", cwd=probe))
        name = str(uuid.uuid4())
        self.key_file, self.known_hosts = self.temp / name, self.temp / f"{name}_known_hosts"
        self.credentials = self.temp / f"git-credentials-{uuid.uuid4()}.config"
        self.key_file.write_text(KEY + "\n", encoding="utf-8")
        self.known_hosts.write_text("github.com ssh-ed25519 AAAA\n", encoding="utf-8")
        self.credentials.write_text('[http "https://github.com/"]\n\textraheader = AUTHORIZATION: basic eA==\n',
                                    encoding="utf-8")
        git("config", "core.sshCommand", f'"/usr/bin/ssh" -i "$RUNNER_TEMP/{name}" -o StrictHostKeyChecking=yes '
            f'-o CheckHostIP=no -o "UserKnownHostsFile=$RUNNER_TEMP/{name}_known_hosts"', cwd=probe)
        git("config", f"includeIf.gitdir:{probe}/.git.path", str(self.credentials), cwd=probe)
        return probe

    def command(self, name, **env):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            code = fetch.main([name])
        return code, out.getvalue(), err.getvalue()

    def resolved(self):
        self.checkout_probe()
        self.assertEqual(0, self.command("move")[0])
        code, _, err = self.command("resolve")
        self.assertEqual(0, code, err)
        return self.output.read_text(encoding="utf-8").strip().removeprefix("revision=")

    def members(self):
        with tarfile.open(self.temp / "agents-toolkit.tar") as archive:
            return {member.name: member for member in archive if member.isfile() or member.issym()}

    def stamp(self, member):
        with tarfile.open(self.temp / "agents-toolkit.tar") as archive:
            return archive.extractfile(member).read().decode()

    def tar_commit(self):
        with (self.temp / "agents-toolkit.tar").open("rb") as stream:
            return subprocess.run(["git", "get-tar-commit-id"], stdin=stream, capture_output=True, text=True,
                                  check=True).stdout.strip()

    def assert_no_probe_or_key(self):
        self.assertFalse((self.workspace / ".agents-toolkit-probe").exists())
        self.assertFalse((self.temp / "agents-probe").exists())
        for path in (self.key_file, self.known_hosts, self.credentials):
            self.assertFalse(path.exists(), path.name)


class CheckTest(FetchFixture):
    def test_empty_deploy_key_fails(self):
        self.assertEqual(1, self.command("check", FETCH_DEPLOY_KEY="")[0])

    def test_git_older_than_2_44_fails_and_2_44_passes(self):
        bin_dir = self.root / "fake-bin"
        bin_dir.mkdir()
        (bin_dir / "git").write_text('#!/bin/sh\necho "git version $FAKE_GIT_VERSION"\n', encoding="utf-8")
        (bin_dir / "git").chmod(0o755)
        for version, expected in (("2.43.7", 1), ("2.44.0", 0)):
            with self.subTest(version=version):
                code, _, err = self.command("check", FAKE_GIT_VERSION=version,
                                            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
                self.assertEqual(expected, code, err)

    def test_upload_requires_a_private_repository_and_fails_closed_on_the_api(self):
        def answer(value):
            def read(request, timeout):
                self.requests.append(request.full_url)
                if isinstance(value, Exception):
                    raise value
                return Response(value)
            return read

        forbidden = HTTPError(f"{API}/repos/{REPOSITORY}", 403, "Forbidden", {}, io.BytesIO())
        cases = {"public event": ("true", "false", offline, 1), "private event": ("true", "true", offline, 0),
                 "public without upload": ("false", "false", offline, 0),
                 "api public": ("true", "", answer({"private": False}), 1),
                 "api private": ("true", "", answer({"private": True}), 0),
                 "api read fails": ("true", "", answer(forbidden), 1),
                 "api omits visibility": ("true", "", answer({"id": 1}), 1)}
        for case, (upload, private, api, expected) in cases.items():
            with self.subTest(case):
                self.requests, self.api = [], api
                code, _, err = self.command("check", FETCH_UPLOAD=upload, FETCH_REPOSITORY_PRIVATE=private)
                self.assertEqual(expected, code, err)
                if not private:
                    self.assertEqual([f"{API}/repos/{REPOSITORY}"], self.requests)


class ResolveTest(FetchFixture):
    def test_push_event_tests_the_default_branch_with_an_empty_body_and_no_api_read(self):
        revision = self.resolved()
        self.assertEqual(self.main, revision)
        self.assertEqual(f"revision={self.main}\n", self.output.read_text(encoding="utf-8"))
        self.assertEqual("", (self.temp / "agents-toolkit-pr-body.txt").read_text(encoding="utf-8"))

    def test_pull_request_uses_the_live_body_companion_added_after_the_pr_opened(self):
        self.event.write_text(json.dumps({"pull_request": {"number": 7, "body": "Opened without a companion"}}),
                              encoding="utf-8")

        def live(request, timeout):
            self.requests.append(request.full_url)
            return Response({"number": 7, "body": "Fix it\nCompanion: tommygwu/agents#7\n"})

        self.api = live
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            revision = self.resolved()
        self.assertEqual([f"{API}/repos/{REPOSITORY}/pulls/7"], self.requests)
        self.assertEqual(self.companion, revision)

    def test_resolver_runs_on_the_resolver_python_when_one_is_given(self):
        log, wrapper = self.root / "resolver-python.log", self.root / "resolver-python"
        wrapper.write_text(f'#!/bin/sh\necho "$2" >> "{log}"\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        with patch.dict(os.environ, {"FETCH_RESOLVER_PYTHON": str(wrapper)}):
            self.assertEqual(self.main, self.resolved())
        self.assertEqual(["agents-revision"], log.read_text().splitlines())

    def test_resolver_output_without_exactly_one_full_revision_line_fails(self):
        self.checkout_probe()
        self.assertEqual(0, self.command("move")[0])
        resolver = self.temp / "agents-probe" / "scripts" / "companion_source.py"
        outputs = {"two revisions": f"revision={self.main}\\nrevision={self.companion}",
                   "short revision": "revision=abc123", "no revision": "reason=none"}
        for case, printed in outputs.items():
            with self.subTest(case):
                resolver.write_text(f'print("{printed}")\n', encoding="utf-8")
                self.output.write_text("", encoding="utf-8")
                self.assertEqual(1, self.command("resolve")[0])
                self.assertEqual("", self.output.read_text(encoding="utf-8"))


class ArchiveTest(FetchFixture):
    def test_openclaw_profile_holds_the_whole_tree_both_stamps_and_the_revision_commit(self):
        revision = self.resolved()
        code, _, err = self.command("archive", FETCH_REVISION=revision, FETCH_PROFILE="openclaw")
        self.assertEqual(0, code, err)
        members = self.members()
        self.assertEqual(OPENCLAW_MEMBERS, set(members))
        self.assertTrue(members[SYMLINK].issym())
        for stamp in (".candidate-revision", "packages/openclaw-shared/.candidate-revision"):
            self.assertEqual(f"{revision}\n", self.stamp(members[stamp]))
        self.assertEqual(revision, self.tar_commit())

    def test_default_profile_follows_the_companion_revision_path_list(self):
        self.event.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
        self.api = lambda request, timeout: Response({"body": "Companion: tommygwu/agents#7"})
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            revision = self.resolved()
        code, _, err = self.command("archive", FETCH_REVISION=revision)
        self.assertEqual(0, code, err)
        self.assertEqual(COMPANION_MEMBERS, set(self.members()))
        self.assertEqual(self.companion, self.tar_commit())

    def test_hydration_is_one_fetch_and_archive_succeeds_after_the_origin_disappears(self):
        revision = self.resolved()
        log = self.root / "upload-pack.log"
        wrapper = self.root / "upload-pack"
        wrapper.write_text(f'#!/bin/sh\necho fetch >> "{log}"\nexec git upload-pack "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        git("config", "remote.origin.uploadpack", str(wrapper), cwd=self.temp / "agents-probe")
        hydrate, fetches = fetch.hydrate, []

        def hydrate_then_lose_origin(rev):
            before = len(log.read_text().splitlines()) if log.exists() else 0
            hydrate(rev)
            fetches.append(len(log.read_text().splitlines()) - before)
            self.origin.rename(self.root / "agents-gone.git")

        with patch.object(fetch, "hydrate", hydrate_then_lose_origin):
            code, _, err = self.command("archive", FETCH_REVISION=revision)
        self.assertEqual(0, code, err)
        self.assertEqual([1], fetches)
        self.assertEqual(DEFAULT_MEMBERS, set(self.members()))

    def test_tracked_deploy_key_fails_the_archive_leaving_no_tar_and_cleanup_removes_the_rest(self):
        revision = self.resolved()
        notes_key = MAIN_FILES["skills/repo-runtime/notes.txt"].strip()
        partial = self.temp / "agents-toolkit.tar.partial"
        code, _, _ = self.command("archive", FETCH_REVISION=revision, FETCH_DEPLOY_KEY=notes_key)
        self.assertEqual(1, code)
        self.assertFalse((self.temp / "agents-toolkit.tar").exists())
        self.assertTrue(partial.exists())
        code, _, err = self.command("cleanup", FETCH_DEPLOY_KEY=notes_key)
        self.assertEqual(0, code, err)
        self.assert_no_probe_or_key()
        self.assertFalse(partial.exists())


class CleanupTest(FetchFixture):
    def test_cleanup_after_a_passing_archive_removes_probe_and_key_but_keeps_the_tar(self):
        revision = self.resolved()
        self.assertEqual(0, self.command("archive", FETCH_REVISION=revision)[0])
        code, _, err = self.command("cleanup")
        self.assertEqual(0, code, err)
        self.assert_no_probe_or_key()
        self.assertTrue((self.temp / "agents-toolkit.tar").is_file())

    def test_cleanup_deletes_runner_temp_credentials_and_key_copies_but_keeps_outside_paths(self):
        probe = self.checkout_probe()
        outside = self.root / "outside_known_hosts"
        outside.write_text("kept\n", encoding="utf-8")
        command = git("config", "core.sshCommand", cwd=probe)
        git("config", "core.sshCommand", f'{command} -o "UserKnownHostsFile={outside}"', cwd=probe)
        copy, unrelated = self.temp / "copied-key", self.temp / "unrelated.txt"
        copy.write_text(f"\n{KEY}\n", encoding="utf-8")
        unrelated.write_text("not a key\n", encoding="utf-8")
        code, _, err = self.command("cleanup")
        self.assertEqual(0, code, err)
        self.assert_no_probe_or_key()
        self.assertFalse(copy.exists())
        self.assertTrue(outside.exists())
        self.assertTrue(unrelated.exists())

    def test_cleanup_with_an_empty_key_leaves_empty_files_alone(self):
        empty = self.temp / "empty-file"
        empty.touch()
        code, _, err = self.command("cleanup", FETCH_DEPLOY_KEY="")
        self.assertEqual(0, code, err)
        self.assertTrue(empty.exists())

    def test_cleanup_finishes_every_removal_when_a_probe_config_is_unreadable(self):
        probe = self.checkout_probe()
        shutil.copytree(probe, self.temp / "agents-probe", symlinks=True)
        (probe / ".git" / "config").write_text("[core\n\tbroken =\n", encoding="utf-8")
        copy = self.temp / "copied-key"
        copy.write_text(KEY, encoding="utf-8")
        self.assertEqual(1, self.command("cleanup")[0])
        self.assert_no_probe_or_key()
        self.assertFalse(copy.exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_cleanup_reports_a_credentials_file_it_cannot_remove_and_removes_the_rest(self):
        probe = self.checkout_probe()
        locked = self.temp / "locked"
        locked.mkdir()
        stuck = locked / "git-credentials-stuck.config"
        stuck.write_text("[http]\n", encoding="utf-8")
        git("config", f"includeIf.gitdir:{self.root}/other/.git.path", str(stuck), cwd=probe)
        copy = self.temp / "copied-key"
        copy.write_text(KEY, encoding="utf-8")
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        code, _, err = self.command("cleanup")
        self.assertEqual(1, code)
        self.assertIn(str(stuck), err)
        self.assertTrue(stuck.exists())
        self.assert_no_probe_or_key()
        self.assertFalse(copy.exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_cleanup_fails_while_a_key_copy_survives(self):
        copy = self.temp / "copied-key"
        copy.write_text(KEY, encoding="utf-8")
        self.temp.chmod(0o500)
        self.addCleanup(self.temp.chmod, 0o700)
        self.assertEqual(1, self.command("cleanup")[0])
        self.assertTrue(copy.exists())


if __name__ == "__main__":
    unittest.main()
