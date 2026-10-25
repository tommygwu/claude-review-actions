import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from run_context import validate

ACTION = Path(__file__).resolve().parent
FIXTURES = ACTION / 'fixtures'


def fixture(name, kind='event'):
    return json.loads((FIXTURES / (name + '.' + kind + '.json')).read_text())


class CaptureCLI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.event_path = self.root / 'event.json'
        self.output = self.root / 'artifact' / 'context.json'
        self.event_path.write_text(json.dumps(fixture('pull_request')))
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(('GITHUB_', 'CI_CONTEXT_'))}
        self.env.update(fixture('pull_request', 'environment'))
        self.env.update(GITHUB_EVENT_PATH=str(self.event_path), GITHUB_TOKEN='SYNTHETIC-PRIVATE-TOKEN', PYTHONDONTWRITEBYTECODE='1')

    def capture(self, env=None, output=None):
        return subprocess.run([sys.executable, str(ACTION / 'capture.py'), str(output or self.output)],
                              cwd=self.root, env=env or self.env, capture_output=True, text=True)

    def test_cli_captures_without_caller_checkout_and_writes_one_valid_member(self):
        source = self.event_path.read_bytes()
        result = self.capture()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('', result.stdout)
        actual = json.loads(self.output.read_text())
        validate(actual)
        expected = fixture('pull_request', 'expected-context')
        self.assertTrue(actual['observed_at'])
        actual['observed_at'] = expected['observed_at']
        self.assertEqual(expected, actual)
        self.assertEqual(['context.json'], [path.name for path in self.output.parent.iterdir()])
        self.assertEqual(source, self.event_path.read_bytes())
        self.assertNotIn('SYNTHETIC-PRIVATE-TOKEN', self.output.read_text())
        self.assertFalse((self.root / '.git').exists())

    def test_cli_bad_event_and_encoding_fail_before_output_without_disclosing_content(self):
        for payload in [b'{"token":"SYNTHETIC-PRIVATE-TOKEN",', b'\xff', b'[]', b'{"pull_request":{"number":true,"body":"SYNTHETIC-PRIVATE-TOKEN"}}']:
            with self.subTest(payload=payload):
                self.event_path.write_bytes(payload)
                result = self.capture()
                self.assertNotEqual(0, result.returncode)
                self.assertEqual('', result.stdout)
                self.assertRegex(result.stderr.strip(), r'^run-context-[a-z-]+(?::[A-Za-z0-9_.\[\]-]+)*$')
                self.assertNotIn('SYNTHETIC', result.stderr)
                self.assertNotIn(str(self.root), result.stderr)
                self.assertFalse(self.output.exists())

    def test_cli_missing_environment_or_event_path_is_safe_and_has_no_output(self):
        for field in ['GITHUB_EVENT_PATH', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_EVENT_NAME', 'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_SHA', 'GITHUB_REF']:
            with self.subTest(missing=field):
                env = dict(self.env)
                del env[field]
                result = self.capture(env=env)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual('run-context-environment-missing', result.stderr.strip())
                self.assertFalse(self.output.exists())
        self.env['GITHUB_EVENT_PATH'] = str(self.root / 'SYNTHETIC-PRIVATE-PATH')
        result = self.capture()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('run-context-event-unreadable', result.stderr.strip())
        self.assertFalse(self.output.exists())

    def test_cli_unwritable_destination_has_safe_error(self):
        blocker = self.root / 'SYNTHETIC-PRIVATE-PATH'
        blocker.write_text('not a directory')
        result = self.capture(output=blocker / 'context.json')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('run-context-output-unwritable', result.stderr.strip())
        self.assertEqual('', result.stdout)

    def test_cli_never_writes_an_artifact_larger_than_64_kib(self):
        expected = fixture('workflow_run', 'expected-context')
        expected['observed_at'] = '2026-10-25T00:00:00.000000+00:00'
        template = fixture('pull_request', 'expected-context')['pull_request']
        expected['parent']['pull_requests'] = [json.loads(json.dumps(template)) for _ in range(100)]
        for entry in expected['parent']['pull_requests']:
            remaining = 65536 - len(json.dumps(expected).encode('utf-8'))
            entry['head']['repository'] += 'r' * min(max(0, remaining), 200 - len(entry['head']['repository']))
            remaining = 65536 - len(json.dumps(expected).encode('utf-8'))
            entry['base']['repository'] += 'r' * min(max(0, remaining // 2), 200 - len(entry['base']['repository']))
            entry['repository'] = entry['base']['repository']
        remaining = 65536 - len(json.dumps(expected).encode('utf-8'))
        expected['workflow_ref'] += 'x' * remaining
        self.assertEqual(65536, len(json.dumps(expected).encode('utf-8')))
        event = fixture('workflow_run')
        event['workflow_run']['pull_requests'] = []
        for entry in expected['parent']['pull_requests']:
            event['workflow_run']['pull_requests'].append({
                'number': entry['number'], 'merge_commit_sha': entry['merge_commit_sha'],
                'head': {'sha': entry['head']['sha'], 'repo': {'full_name': entry['head']['repository']}},
                'base': {'sha': entry['base']['sha'], 'repo': {'full_name': entry['base']['repository']}}})
        self.event_path.write_text(json.dumps(event))
        self.env.update(fixture('workflow_run', 'environment'))
        self.env['GITHUB_WORKFLOW_REF'] = expected['workflow_ref']
        result = self.capture()
        if result.returncode == 0:
            self.assertLessEqual(self.output.stat().st_size, 65536)
        else:
            self.assertEqual('run-context-too-large', result.stderr.strip())
            self.assertFalse(self.output.exists())

    def test_composite_executes_declared_capture_and_uploads_that_single_member(self):
        steps = composite_steps((ACTION / 'action.yml').read_text())
        self.assertEqual(2, len(steps))
        capture, upload = steps
        self.assertEqual('bash', capture['shell'])
        self.assertEqual('actions/upload-artifact@v7', upload['uses'])
        self.assertEqual('error', upload['with']['if-no-files-found'])
        action_link = self.root / 'public action'
        action_link.symlink_to(ACTION, target_is_directory=True)
        del self.env['GITHUB_TOKEN']
        values = {'github.action_path': str(action_link), 'github.workflow_ref': self.env['GITHUB_WORKFLOW_REF'],
                  'github.workflow_sha': self.env['GITHUB_WORKFLOW_SHA'], 'github.run_attempt': '3',
                  'runner.temp': str(self.root / 'runner temp')}
        self.env['RUNNER_TEMP'] = values['runner.temp']
        del self.env['GITHUB_WORKFLOW_REF']
        del self.env['GITHUB_WORKFLOW_SHA']
        self.env.update({key: render(value, values) for key, value in capture.get('env', {}).items()})
        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', render(capture['run'], values)],
                                cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        artifact = Path(render(upload['with']['path'], values))
        self.assertTrue(artifact.is_file())
        self.assertEqual('ci-run-context-3', render(upload['with']['name'], values))
        self.assertEqual('context.json', artifact.name)
        self.assertEqual([artifact], list(artifact.parent.rglob('*')))
        actual = json.loads(artifact.read_text())
        validate(actual)
        expected = fixture('pull_request', 'expected-context')
        actual['observed_at'] = expected['observed_at']
        self.assertEqual(expected, actual)


def render(value, values):
    return re.sub(r'\$\{\{\s*([a-z_.]+)\s*\}\}', lambda match: values[match.group(1)], value)


def composite_steps(source):
    steps = []
    active = None
    section = None
    composite = False
    for line in source.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line == '  using: composite':
            composite = True
        match = re.fullmatch(r'    - ([a-z-]+): (.*)', line)
        if match:
            active = {match.group(1): match.group(2)}
            steps.append(active)
            section = None
            continue
        match = re.fullmatch(r'      ([a-z-]+):(?: (.*))?', line)
        if match and active is not None:
            key, value = match.groups()
            section = key if value is None else None
            active[key] = {} if value is None else value
            continue
        match = re.fullmatch(r'        ([A-Za-z_-]+): (.*)', line)
        if match and active is not None and section:
            active[section][match.group(1)] = match.group(2)
            continue
        if active is not None:
            raise AssertionError('Unsupported composite syntax: ' + line)
    if not composite:
        raise AssertionError('Action must run as a composite')
    return steps


if __name__ == '__main__':
    unittest.main()
