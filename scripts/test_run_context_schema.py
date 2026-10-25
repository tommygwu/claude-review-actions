import json
from pathlib import Path
import subprocess
import sys
import unittest

from jsonschema import Draft202012Validator

ACTION = Path(__file__).resolve().parents[1] / '.github/actions/ci-run-context'
sys.path.insert(0, str(ACTION))
from run_context import extract, validate

PR_PATHS = [('pull_request',), ('parent', 'pull_requests', 0)]
REPOSITORY_PATHS = [('repository',)] + [
    prefix + suffix for prefix in PR_PATHS
    for suffix in [('repository',), ('head', 'repository'), ('base', 'repository')]
]
SHA_PATHS = [('workflow_sha',), ('actions_sha',), ('parent', 'head_sha')] + [
    prefix + suffix for prefix in PR_PATHS
    for suffix in [('head', 'sha'), ('base', 'sha'), ('merge_commit_sha',)]
]
INVALID_REPOSITORIES = ['owner/repo/extra', 'owner/repo\n', '\nowner/repo',
                        'owner/rep\no', ' owner/repo', 'owner/repo ']
INVALID_SHAS = ['a' * 39, 'a' * 41, 'x' + 'a' * 40, 'a' * 40 + 'x',
                'a' * 40 + '\n', '\n' + 'a' * 40, 'a' * 20 + '\n' + 'a' * 20,
                'a' * 40 + '\r\n', 'a' * 40 + '\u2028']


def at_path(value, path):
    for key in path:
        value = value[key]
    return value


class PublishedSchemaContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ACTION / 'run-context.schema.json').read_text())
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def context(self):
        result = json.loads((ACTION / 'fixtures/workflow_run.expected-context.json').read_text())
        result['pull_request'] = json.loads((ACTION / 'fixtures/pull_request.expected-context.json').read_text())['pull_request']
        return result

    def changed(self, path, value):
        result = self.context()
        at_path(result, path[:-1])[path[-1]] = value
        return result

    def test_published_schema_accepts_all_canonical_fixture_outputs(self):
        for path in sorted((ACTION / 'fixtures').glob('*.expected-context.json')):
            with self.subTest(fixture=path.name):
                context = json.loads(path.read_text())
                self.validator.validate(context)
                validate(context)

    def test_real_workflow_run_reference_capture_passes_the_published_schema(self):
        event = json.loads((ACTION / 'fixtures/workflow_run.event.json').read_text())
        env = json.loads((ACTION / 'fixtures/workflow_run.environment.json').read_text())
        actual = extract(event, env, '2026-10-25T00:00:00Z')
        self.validator.validate(actual)
        self.assertEqual(json.loads((ACTION / 'fixtures/workflow_run.expected-context.json').read_text()), actual)

    def test_positive_identities_and_required_nullable_members_keep_their_contract(self):
        for paths, valid in [(REPOSITORY_PATHS, 'Owner.Name-1/repo_name.2'),
                             (SHA_PATHS, 'aB09' * 10)]:
            for path in paths:
                with self.subTest(path=path, value=valid):
                    context = self.changed(path, valid)
                    self.validator.validate(context)
                    validate(context)
        for path in REPOSITORY_PATHS[1:] + SHA_PATHS[2:] + [('pull_request',), ('parent',), ('action',),
                                                                  ('parent', 'run_attempt'), ('parent', 'workflow_id')]:
            with self.subTest(nullable=path):
                context = self.changed(path, None)
                self.validator.validate(context)
                validate(context)
                del at_path(context, path[:-1])[path[-1]]
                self.assertFalse(self.validator.is_valid(context))
                with self.assertRaises(ValueError):
                    validate(context)

    def test_published_schema_and_producer_reject_invalid_identities_at_every_boundary(self):
        for paths, invalids in [(REPOSITORY_PATHS, INVALID_REPOSITORIES),
                                (SHA_PATHS, INVALID_SHAS)]:
            for path in paths:
                for invalid in invalids:
                    with self.subTest(path=path, invalid=invalid):
                        context = self.changed(path, invalid)
                        errors = list(self.validator.iter_errors(context))
                        self.assertTrue(any(error.validator == 'pattern' and tuple(error.path) == path
                                            for error in errors), errors)
                        with self.assertRaises(ValueError):
                            validate(context)

    def test_published_patterns_use_portable_full_string_matching_in_javascript(self):
        cases = []

        def visit(node):
            if isinstance(node, dict):
                if 'pattern' in node:
                    repository = '/' in node['pattern']
                    cases.append({'pattern': node['pattern'],
                                  'valid': ['Owner.Name-1/repo_name.2'] if repository else ['aB09' * 10],
                                  'invalid': INVALID_REPOSITORIES if repository else INVALID_SHAS})
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(self.schema)
        completed = subprocess.run(['node', '--input-type=module', '-e', '''
import assert from 'node:assert/strict';
let input = '';
for await (const chunk of process.stdin) input += chunk;
for (const {pattern, valid, invalid} of JSON.parse(input)) {
  const regex = new RegExp(pattern);
  for (const value of valid) assert.equal(regex.test(value), true, pattern);
  for (const value of invalid) assert.equal(regex.test(value), false, pattern + ': ' + JSON.stringify(value));
}
'''], input=json.dumps(cases), text=True, capture_output=True)
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == '__main__':
    unittest.main()
