"""Restore fixture toolkits with a fake downloaded mise that records its argv; no network, no real mise or sudo."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import restore

RUNTIME_VERSIONS = ".github/actions/mise-python/runtime-versions.sh"
REVISION = "a" * 40
FAKE_MISE = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_MISE_STATE"])
with open(state / "calls.jsonl", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
installed_file = state / "installed.json"
installed = json.loads(installed_file.read_text()) if installed_file.exists() else []
command, spec = sys.argv[1], sys.argv[-1]
if sys.argv[1:] == ["ls", "--installed", "python", "--json"]:
    print(json.dumps([{{"version": version}} for version in installed]))
elif command == "install":
    patch = spec.split("@")[1] + ".9"
    installed_file.write_text(json.dumps(sorted(set(installed) | {{patch}})))
elif command == "uninstall":
    installed_file.write_text(json.dumps([v for v in installed if v != spec.split("@")[1]]))
elif command == "where":
    name, version = spec.split("@")
    folder = state / "installs" / name / version
    if name == "python" and version in installed:
        interpreter = folder / "bin" / ("python" + ".".join(version.split(".")[:2]))
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_text("")
        interpreter.chmod(0o755)
    print(folder)
else:
    sys.exit("unexpected mise call")
"""
MISE_BYTES = FAKE_MISE.encode()
MISE_SHA = hashlib.sha256(MISE_BYTES).hexdigest()


def runtime_versions(version="2026.9.15", asset="linux-x64", sha=MISE_SHA):
    return f"CI_MISE_VERSION={version}\nCI_MISE_ASSET={asset}\nCI_MISE_SHA256={sha}\n"


def toolkit(node="26.1.0", python="3.13", pnpm="pnpm@10.1.0", mise="2026.9.15", name="agents"):
    return {
        ".candidate-revision": f"{REVISION}\n",
        ".node-version": f"{node}\n",
        ".python-version": f"{python}\n",
        "package.json": json.dumps({"name": name, "packageManager": pnpm}),
        RUNTIME_VERSIONS: runtime_versions(version=mise),
        "skills/repo-runtime/scripts/repo-runtime": "#!/bin/sh\nexit 0\n",
    }


class RestoreFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.temp, self.state = self.root / "runner-temp", self.root / "mise-state"
        self.temp.mkdir()
        self.state.mkdir()
        self.files = {name: self.root / name for name in ("github-output", "github-env", "github-path")}
        environment = patch.dict(os.environ, {
            "RUNNER_TEMP": str(self.temp), "GITHUB_OUTPUT": str(self.files["github-output"]),
            "GITHUB_ENV": str(self.files["github-env"]), "GITHUB_PATH": str(self.files["github-path"]),
            "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64", "RUNNER_ENVIRONMENT": "github-hosted",
            "FAKE_MISE_STATE": str(self.state), "INPUT_CACHE": "true", "INPUT_SAVE": "false",
            "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/1/merge", "DEFAULT_BRANCH": "main",
            "CACHE_SAVE": "false", "HOST_LINK": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.downloads = []
        self.mise_bytes = MISE_BYTES
        download = patch.object(restore, "download", lambda url: self.downloads.append(url) or self.mise_bytes)
        download.start()
        self.addCleanup(download.stop)

    def pack(self, files, executable=("skills/repo-runtime/scripts/repo-runtime",)):
        with tarfile.open(self.temp / "agents-toolkit.tar", "w") as archive:
            for name, content in files.items():
                data = content.encode()
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o755 if name in executable else 0o644
                archive.addfile(member, io.BytesIO(data))

    def command(self, name, **env):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            code = restore.main([name])
        return code, err.getvalue()

    def ok(self, name, **env):
        code, err = self.command(name, **env)
        self.assertEqual(0, code, err)

    def outputs(self):
        path = self.files["github-output"]
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return dict(line.split("=", 1) for line in lines)

    def exported(self):
        path = self.files["github-env"]
        return [line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def mise_calls(self):
        log = self.state / "calls.jsonl"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [call for call in calls if call[0] in ("install", "uninstall", "where")]

    def install_node(self, pin, mode=0o755):
        node = self.state / "installs" / "node" / pin / "bin" / "node"
        node.parent.mkdir(parents=True, exist_ok=True)
        node.write_text("")
        node.chmod(mode)
        return node

    def installed(self, *patches):
        (self.state / "installed.json").write_text(json.dumps(list(patches)))

    def unpacked(self, files):
        self.pack(files)
        self.ok("unpack")


class UnpackTest(RestoreFixture):
    def test_missing_artifact_fails_naming_it_and_asking_for_a_full_rerun(self):
        code, err = self.command("unpack")
        self.assertEqual(1, code)
        self.assertIn("agents-toolkit", err)
        self.assertIn("re-run all jobs", err)

    def test_stamp_must_be_one_40_hex_line(self):
        stamps = {"missing": None, "two lines": f"{REVISION}\n{REVISION}\n", "short": "abc123\n",
                  "uppercase": f"{'A' * 40}\n"}
        for case, stamp in stamps.items():
            with self.subTest(case):
                files = {name: content for name, content in toolkit().items() if name != ".candidate-revision"}
                if stamp is not None:
                    files[".candidate-revision"] = stamp
                self.pack(files)
                self.assertEqual(1, self.command("unpack")[0])

    def test_valid_toolkit_unpacks_at_the_fixed_location_with_executable_modes(self):
        self.unpacked(toolkit())
        self.assertTrue(os.access(self.temp / "agents-toolkit/skills/repo-runtime/scripts/repo-runtime", os.X_OK))


class PlanTest(RestoreFixture):
    def prefix(self, files):
        self.unpacked(files)
        self.files["github-output"].unlink(missing_ok=True)
        self.ok("plan")
        return self.outputs()["prefix"]

    def test_each_pin_changes_the_cache_prefix_and_other_package_fields_do_not(self):
        baseline = self.prefix(toolkit())
        self.assertRegex(baseline, r"\Aagents-toolkit-Linux-X64-[0-9a-f]{24}-\Z")
        for pin, files in {"node": toolkit(node="26.2.0"), "python": toolkit(python="3.14"),
                           "pnpm": toolkit(pnpm="pnpm@10.2.0"), "mise": toolkit(mise="2026.10.1")}.items():
            with self.subTest(pin):
                self.assertNotEqual(baseline, self.prefix(files))
        self.assertEqual(baseline, self.prefix(toolkit(name="renamed")))

    def test_restore_and_save_follow_the_cache_switch_runner_and_trusted_events(self):
        self.unpacked(toolkit())
        trusted = {"GITHUB_EVENT_NAME": "schedule", "INPUT_SAVE": "true"}
        cases = {
            "cache off on a trusted event": ({**trusted, "INPUT_CACHE": "false"}, "false", "false"),
            "schedule": (trusted, "true", "true"),
            "default-branch push": ({**trusted, "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"},
                                    "true", "true"),
            "other-branch push": ({**trusted, "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/feature"},
                                  "true", "false"),
            "push without a default branch": ({**trusted, "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/",
                                               "DEFAULT_BRANCH": ""}, "true", "false"),
            "pull request": ({**trusted, "GITHUB_EVENT_NAME": "pull_request"}, "true", "false"),
            "save off": ({**trusted, "INPUT_SAVE": "false"}, "true", "false"),
            "self-hosted": ({**trusted, "RUNNER_ENVIRONMENT": "self-hosted"}, "false", "false"),
        }
        for case, (env, restore_cache, save_cache) in cases.items():
            with self.subTest(case):
                self.files["github-output"].unlink(missing_ok=True)
                self.ok("plan", **env)
                self.assertEqual((restore_cache, save_cache), (self.outputs()["restore"], self.outputs()["save"]))

    def test_missing_pin_file_fails_naming_it(self):
        for name in (".node-version", ".python-version", "package.json", RUNTIME_VERSIONS):
            with self.subTest(name):
                self.pack({key: value for key, value in toolkit().items() if key != name})
                self.ok("unpack")
                code, err = self.command("plan")
                self.assertEqual(1, code)
                self.assertIn(name, err)


class MiseTest(RestoreFixture):
    def test_toolkit_pins_drive_mise_python_node_and_the_four_exports(self):
        cases = {"a": (toolkit(), "2026.9.15", "3.13", "26.1.0"),
                 "b": (toolkit(node="26.2.0", python="3.14", pnpm="pnpm@10.2.0", mise="2026.10.1"),
                       "2026.10.1", "3.14", "26.2.0")}
        prefixes = set()
        for case, (files, mise, series, node_pin) in cases.items():
            with self.subTest(case):
                for path in (*self.files.values(), self.state / "calls.jsonl"):
                    path.unlink(missing_ok=True)
                self.downloads.clear()
                node = self.install_node(node_pin)
                self.unpacked(files)
                for step in ("plan", "mise", "python", "export"):
                    self.ok(step)
                prefixes.add(self.outputs()["prefix"])
                self.assertEqual([f"https://github.com/jdx/mise/releases/download/v{mise}/mise-v{mise}-linux-x64"],
                                 self.downloads)
                self.assertEqual([["install", f"python@{series}"], ["where", f"python@{series}.9"],
                                  ["where", f"node@{node_pin}"]], self.mise_calls())
                self.assertEqual([["REPO_RUNTIME_MISE_BIN", str(self.temp / "mise/bin/mise")],
                                  ["AGENTS_REPO", str(self.temp / "agents-toolkit")],
                                  ["AGENTS_NODE", str(node)],
                                  ["AGENTS_ARCHIVE", str(self.temp / "agents-toolkit.tar")]], self.exported())
                self.assertFalse(self.files["github-path"].exists())
        self.assertEqual(2, len(prefixes))

    def test_checksum_mismatch_fails_before_install(self):
        self.unpacked(toolkit())
        self.mise_bytes = b"#!/bin/sh\necho tampered\n"
        self.assertEqual(1, self.command("mise")[0])
        self.assertFalse((self.temp / "mise").exists())

    def test_unlisted_platform_fails_naming_runtime_versions_without_downloading(self):
        self.unpacked({**toolkit(), RUNTIME_VERSIONS: "return 2\n"})
        code, err = self.command("mise")
        self.assertEqual(1, code)
        self.assertIn(RUNTIME_VERSIONS, err)
        self.assertEqual([], self.downloads)


class PythonTest(RestoreFixture):
    def test_install_follows_installed_patches_runner_and_saving(self):
        cases = {
            "hosted reuses an installed patch": ({}, ("3.14.1",), [], "3.14.1"),
            "hosted installs when none is installed": ({}, (), [["install", "python@3.14"]], "3.14.9"),
            "self-hosted always installs": ({"RUNNER_ENVIRONMENT": "self-hosted"}, ("3.14.1",),
                                            [["install", "python@3.14"]], "3.14.9"),
            "saving installs and removes older patches": (
                {"CACHE_SAVE": "true"}, ("3.13.5", "3.14.1"),
                [["install", "python@3.14"], ["uninstall", "python@3.14.1"]], "3.14.9"),
        }
        for case, (env, patches, changes, patch_version) in cases.items():
            with self.subTest(case):
                for path in (self.files["github-output"], self.state / "calls.jsonl"):
                    path.unlink(missing_ok=True)
                self.unpacked(toolkit(python="3.14"))
                self.ok("mise")
                self.installed(*patches)
                self.ok("python", **env)
                self.assertEqual(changes, [call for call in self.mise_calls() if call[0] != "where"])
                self.assertEqual(patch_version, self.outputs()["patch"])
        self.assertIn("3.13.5", json.loads((self.state / "installed.json").read_text()))

    def test_python_version_must_be_a_major_minor_series(self):
        self.unpacked(toolkit(python="3.14.1"))
        self.ok("mise")
        self.assertEqual(1, self.command("python")[0])
        self.assertEqual([], self.mise_calls())


class ExportTest(RestoreFixture):
    def test_non_executable_toolkit_node_fails_without_exporting(self):
        self.install_node("26.1.0", mode=0o644)
        self.unpacked(toolkit())
        self.ok("mise")
        self.assertEqual(1, self.command("export")[0])
        self.assertEqual([], self.exported())


class HostLinkTest(RestoreFixture):
    def setUp(self):
        super().setUp()
        self.unpacked({**toolkit(), "packages/openclaw-shared/README.md": "shared\n"})
        self.users = self.root / "Users"
        (self.users / "Shared").mkdir(parents=True)
        self.host_root = self.users / "Shared" / "agents"
        self.mountinfo = self.root / "mountinfo"
        self.mount("/")
        self.sudo = []
        real_run = subprocess.run

        def sudo(command, **kwargs):
            self.assertEqual("sudo", command[0])
            self.sudo.append(command[1:3])
            return real_run(command[1:], **kwargs)

        for patcher in (patch.object(restore, "MOUNTINFO", self.mountinfo), patch.object(restore, "USERS", self.users),
                        patch.object(restore, "HOST_ROOT", self.host_root),
                        patch.object(restore.subprocess, "run", sudo)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def mount(self, *points):
        self.mountinfo.write_text("".join(f"{index} 1 8:1 / {point} rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
                                          for index, point in enumerate(points, 20)), encoding="utf-8")

    def test_refuses_host_reach_unsafe_subpaths_missing_sources_and_real_targets(self):
        def users_mount():
            self.mount("/", str(self.users))

        def nested_mount():
            self.mount("/", f"{self.users}/Shared/agents")

        def linked_users():
            self.users.rename(self.root / "real-users")
            self.users.symlink_to(self.root / "real-users")

        def unreadable():
            self.mountinfo.unlink()

        def real_target():
            (self.host_root / "packages" / "openclaw-shared").mkdir(parents=True)

        hosted, self_hosted = "github-hosted", "self-hosted"
        cases = {"self-hosted with a /Users mount": (self_hosted, "packages/openclaw-shared", users_mount),
                 "self-hosted with a mount under /Users": (self_hosted, "packages/openclaw-shared", nested_mount),
                 "self-hosted with a symlinked /Users": (self_hosted, "packages/openclaw-shared", linked_users),
                 "self-hosted with unreadable mountinfo": (self_hosted, "packages/openclaw-shared", unreadable),
                 "absolute subpath": (hosted, str(self.temp / "agents-toolkit/packages/openclaw-shared"), None),
                 "parent subpath": (hosted, "packages/../..", None),
                 "missing source": (hosted, "packages/absent", None),
                 "existing real target": (hosted, "packages/openclaw-shared", real_target)}
        for case, (runner, subpath, arrange) in cases.items():
            with self.subTest(case):
                self.sudo.clear()
                try:
                    if arrange:
                        arrange()
                    self.assertEqual(1, self.command("host-link", RUNNER_ENVIRONMENT=runner, HOST_LINK=subpath)[0])
                    self.assertEqual([], self.sudo)
                finally:
                    if self.users.is_symlink():
                        self.users.unlink()
                        (self.root / "real-users").rename(self.users)
                    shutil.rmtree(self.host_root, ignore_errors=True)
                    self.mount("/")

    def test_links_on_hosted_runners_and_clean_self_hosted_runners(self):
        cases = {"hosted ignores mountinfo": ("github-hosted", "packages/openclaw-shared", (str(self.users),)),
                 "self-hosted without a /Users mount": ("self-hosted", "packages/openclaw-shared", ("/", "/home")),
                 "whole toolkit": ("github-hosted", ".", ())}
        for case, (runner, subpath, mounts) in cases.items():
            with self.subTest(case):
                self.mount(*mounts)
                self.sudo.clear()
                self.ok("host-link", RUNNER_ENVIRONMENT=runner, HOST_LINK=subpath)
                target = self.host_root if subpath == "." else self.host_root / subpath
                self.assertEqual((self.temp / "agents-toolkit" / subpath).resolve(), target.resolve())
                self.assertEqual([["mkdir", "-p"], ["ln", "-sfn"]], self.sudo)
                target.unlink()
                shutil.rmtree(self.host_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
