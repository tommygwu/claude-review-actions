from copy import deepcopy
import json
from pathlib import Path
import unittest

from run_context import extract, validate


class CaptureRegression(unittest.TestCase):
    def test_public_capture_keeps_distinct_revisions_and_excludes_text(self):
        env = {"GITHUB_REPOSITORY": "example/widget", "GITHUB_RUN_ID": "101",
               "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_NAME": "pull_request",
               "GITHUB_SHA": "a" * 40, "GITHUB_REF": "refs/pull/7/merge",
               "GITHUB_WORKFLOW_REF": "example/widget/.github/workflows/test.yml@refs/pull/7/merge",
               "GITHUB_WORKFLOW_SHA": "d" * 40}
        payload = {"action": "synchronize", "pull_request": {"number": 7,
            "body": "secret body", "head": {"sha": "b" * 40, "repo": {"full_name": "contributor/fork"}},
            "base": {"sha": "c" * 40, "repo": {"full_name": "example/widget"}}}}
        result = extract(payload, env, "2026-10-25T00:00:00Z")
        self.assertEqual(("a" * 40, "d" * 40, "b" * 40, "c" * 40),
                         (result["actions_sha"], result["workflow_sha"], result["pull_request"]["head"]["sha"], result["pull_request"]["base"]["sha"]))
        self.assertNotIn("secret body", json.dumps(result))


class ContextContract(unittest.TestCase):
    fixtures = Path(__file__).parent / 'fixtures'

    def fixture(self, name, kind='expected-context'):
        return json.loads((self.fixtures / (name + '.' + kind + '.json')).read_text())

    def rich_context(self):
        result = self.fixture('workflow_run')
        result['pull_request'] = self.fixture('pull_request')['pull_request']
        return result

    def test_literal_event_contracts_cover_every_event_family(self):
        for path in sorted(self.fixtures.glob('*.event.json')):
            name = path.name.removesuffix('.event.json')
            with self.subTest(event=name):
                actual = extract(self.fixture(name, 'event'), self.fixture(name, 'environment'), '2026-10-25T00:00:00Z')
                self.assertEqual(self.fixture(name), actual)
                validate(actual)

    def test_source_event_and_environment_are_neither_mutated_nor_retained(self):
        event = self.fixture('workflow_run', 'event')
        env = self.fixture('workflow_run', 'environment')
        event_before, env_before = deepcopy(event), deepcopy(env)
        result = extract(event, env, '2026-10-25T00:00:00Z')
        self.assertEqual(event_before, event)
        self.assertEqual(env_before, env)
        frozen = deepcopy(result)
        event['workflow_run']['pull_requests'][0]['head']['repo']['url'] = 'https://api.github.com/repos/changed/source'
        event.clear()
        env.clear()
        self.assertEqual(frozen, result)

    def test_sensitive_fields_are_allowlisted_at_each_source_boundary(self):
        event = self.fixture('workflow_run', 'event')
        secret = 'SYNTHETIC-PRIVATE-TOKEN'
        event['token'] = secret
        event['workflow_run']['secret'] = secret
        for pr in event['workflow_run']['pull_requests']:
            pr['body'] = secret
            pr.setdefault('head', {})['token'] = secret
            pr.setdefault('base', {})['repo'] = dict(pr.get('base', {}).get('repo', {}), token=secret)
        env = dict(self.fixture('workflow_run', 'environment'), GITHUB_TOKEN=secret, PRIVATE_SECRET=secret)
        result = extract(event, env, '2026-10-25T00:00:00Z')
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(self.fixture('workflow_run'), result)

    def test_abbreviated_repository_urls_preserve_fork_and_base_without_observer_inference(self):
        event = self.fixture('workflow_run', 'event')
        pr = event['workflow_run']['pull_requests'][0]
        for side in ['head', 'base']:
            self.assertEqual({'id', 'url', 'name'}, set(pr[side]['repo']))
        result = extract(event, self.fixture('workflow_run', 'environment'), '2026-10-25T00:00:00Z')
        self.assertEqual(self.fixture('workflow_run'), result)
        self.assertEqual('contributor/fork', result['parent']['pull_requests'][0]['head']['repository'])
        self.assertEqual('example/widget', result['parent']['pull_requests'][0]['repository'])
        for repo in [None, {}, {'name': 'fork'}, {'id': 1001, 'name': 'fork', 'url': None}]:
            with self.subTest(repo=repo):
                missing = deepcopy(event)
                for side in ['head', 'base']:
                    missing['workflow_run']['pull_requests'][0][side]['repo'] = repo
                context = extract(missing, self.fixture('workflow_run', 'environment'), '2026-10-25T00:00:00Z')
                actual = context['parent']['pull_requests'][0]
                self.assertIsNone(actual['repository'])
                self.assertIsNone(actual['head']['repository'])
                self.assertIsNone(actual['base']['repository'])

    def test_repository_urls_reject_malformed_unsupported_and_conflicting_identity(self):
        invalid_urls = [True, 42, '', 'http://api.github.com/repos/owner/repo',
                        'https://example.test/repos/owner/repo', 'https://api.github.com.evil.test/repos/owner/repo',
                        'https://user@api.github.com/repos/owner/repo', 'https://api.github.com:443/repos/owner/repo',
                        'https://api.github.com/repos/owner/repo/extra', 'https://api.github.com/repos/owner/repo/',
                        'https://api.github.com/repos/owner/repo?x=1', 'https://api.github.com/repos/owner/repo#x',
                        'https://api.github.com/repos/owner/re%70o', 'https://api.github.com/repos/owner/repo\n',
                        'https://api.github.com/repos/owner', 'https://api.github.com/repos//repo']
        invalid_repos = [{'url': url} for url in invalid_urls] + [
            {'url': 'https://api.github.com/repos/owner/repo', 'full_name': 'other/repo'},
            {'url': 'https://api.github.com/repos/owner/repo', 'name': 'other'},
            {'url': 'https://api.github.com/repos/owner/repo', 'full_name': True}]
        for repo in invalid_repos:
            for side in ['head', 'base']:
                with self.subTest(repo=repo, side=side):
                    event = self.fixture('workflow_run', 'event')
                    event['workflow_run']['pull_requests'][0][side]['repo'] = repo
                    with self.assertRaisesRegex(ValueError, '^run-context-repository-(url-invalid|conflict)$'):
                        extract(event, self.fixture('workflow_run', 'environment'), '2026-10-25T00:00:00Z')

    def test_missing_required_members_include_nullable_nested_members(self):
        original = self.rich_context()
        for path in object_paths(original):
            source = at_path(original, path)
            for key in source:
                with self.subTest(path=path, missing=key):
                    changed = deepcopy(original)
                    del at_path(changed, path)[key]
                    with self.assertRaises(ValueError):
                        validate(changed)

    def test_sensitive_extra_fields_are_rejected_at_every_context_boundary(self):
        original = self.rich_context()
        for path in object_paths(original):
            with self.subTest(path=path):
                changed = deepcopy(original)
                at_path(changed, path)['token'] = 'SYNTHETIC-PRIVATE-TOKEN'
                with self.assertRaises(ValueError):
                    validate(changed)

    def test_wrong_member_types_are_rejected_at_every_context_boundary(self):
        original = self.rich_context()
        for path in object_paths(original):
            for key, value in at_path(original, path).items():
                invalid = {'unexpected': 'object'} if not isinstance(value, dict) else []
                with self.subTest(path=path, field=key):
                    changed = deepcopy(original)
                    at_path(changed, path)[key] = invalid
                    with self.assertRaises(ValueError):
                        validate(changed)
        for invalid in [True, 1.0, '1', None]:
            with self.subTest(version=invalid):
                changed = deepcopy(original)
                changed['version'] = invalid
                with self.assertRaises(ValueError):
                    validate(changed)

    def test_nullable_identity_members_remain_valid_when_facts_are_absent(self):
        original = self.rich_context()
        for path in object_paths(original):
            for key in at_path(original, path):
                if key in {'action', 'pull_request', 'parent', 'repository', 'sha', 'merge_commit_sha', 'run_attempt', 'workflow_id', 'head_sha'} and not (path == () and key == 'repository'):
                    with self.subTest(path=path, field=key):
                        changed = deepcopy(original)
                        at_path(changed, path)[key] = None
                        validate(changed)

    def test_positive_integer_identity_fields_reject_bool_zero_negative_and_strings(self):
        original = self.rich_context()
        for path in object_paths(original):
            for key in at_path(original, path):
                if key in {'run_id', 'capture_attempt', 'number', 'run_attempt', 'workflow_id'}:
                    for invalid in [True, False, 0, -1, 1.5, '1']:
                        with self.subTest(path=path, field=key, invalid=invalid):
                            changed = deepcopy(original)
                            at_path(changed, path)[key] = invalid
                            with self.assertRaises(ValueError):
                                validate(changed)

    def test_repository_and_sha_values_are_validated_at_every_identity_boundary(self):
        original = self.rich_context()
        for path in object_paths(original):
            for key in at_path(original, path):
                invalids = ['widget', 'owner/repo/extra', 'owner/with space', 'https://example.test/repo', 'o/' + 'r' * 199] if key == 'repository' else ['a' * 39, 'a' * 41, 'g' * 40, 'a' * 39 + '\n'] if key.endswith('sha') or key == 'sha' else []
                for invalid in invalids:
                    with self.subTest(path=path, field=key, invalid=invalid):
                        changed = deepcopy(original)
                        at_path(changed, path)[key] = invalid
                        with self.assertRaises(ValueError):
                            validate(changed)

    def test_observation_requires_a_valid_timezone_aware_timestamp(self):
        for invalid in ['2026-10-25T00:00:00', '2026-02-30T00:00:00Z', 'not-a-time', '', '2026-10-25']:
            with self.subTest(observed_at=invalid):
                changed = self.rich_context()
                changed['observed_at'] = invalid
                with self.assertRaises(ValueError):
                    validate(changed)
        for valid in ['2026-10-25T00:00:00Z', '2026-10-25T00:00:00-07:00']:
            changed = self.rich_context()
            changed['observed_at'] = valid
            validate(changed)

    def test_string_and_parent_reference_bounds_are_enforced(self):
        for field, maximum in [('event', 100), ('action', 100), ('workflow_ref', 1000), ('actions_ref', 1000)]:
            changed = self.rich_context()
            changed[field] = 'x' * maximum
            validate(changed)
            changed[field] += 'x'
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(changed)
        changed = self.fixture('workflow_run_nullable')
        changed['parent']['pull_requests'] = [self.fixture('incomplete_pull_request')['pull_request']] * 100
        validate(changed)
        changed['parent']['pull_requests'].append(changed['parent']['pull_requests'][0])
        with self.assertRaises(ValueError):
            validate(changed)

    def test_serialized_context_accepts_64_kib_and_rejects_one_byte_more(self):
        changed = self.fixture('workflow_run')
        pr = self.fixture('pull_request')['pull_request']
        changed['parent']['pull_requests'] = [deepcopy(pr) for _ in range(100)]
        for entry in changed['parent']['pull_requests']:
            for node in [entry, entry['head'], entry['base']]:
                remaining = 65536 - len(json.dumps(changed).encode('utf-8'))
                if remaining <= 0:
                    break
                node['repository'] += 'r' * min(remaining, 200 - len(node['repository']))
        self.assertEqual(65536, len(json.dumps(changed).encode('utf-8')))
        validate(changed)
        changed['workflow_ref'] += 'x'
        self.assertEqual(65537, len(json.dumps(changed).encode('utf-8')))
        with self.assertRaisesRegex(ValueError, '^run-context-too-large$'):
            validate(changed)

    def test_malformed_event_containers_do_not_become_missing_facts(self):
        cases = [('pull_request', {'pull_request': []}),
                 ('pull_request_review', {'pull_request': 'invalid'}),
                 ('pull_request', {'pull_request': {'number': 7, 'head': []}}),
                 ('pull_request', {'pull_request': {'number': 7, 'base': {'repo': []}}}),
                 ('issue_comment', {'issue': []}),
                 ('issue_comment', {'issue': {'number': 7, 'pull_request': True}}),
                 ('workflow_run', {'workflow_run': []}),
                 ('workflow_run', {'workflow_run': {'id': 202, 'pull_requests': {}}}),
                 ('workflow_run', {'workflow_run': {'id': 202, 'pull_requests': [None]}})]
        for name, event in cases:
            with self.subTest(event=name, value=event):
                env = dict(self.fixture('push', 'environment'), GITHUB_EVENT_NAME=name)
                with self.assertRaises(ValueError):
                    extract(event, env, '2026-10-25T00:00:00Z')
        for event in [None, [], 'secret event', 3]:
            with self.subTest(top_level=event), self.assertRaises(ValueError):
                extract(event, self.fixture('push', 'environment'), '2026-10-25T00:00:00Z')

    def test_caller_run_identity_rejects_non_decimal_and_nonpositive_environment_ids(self):
        for field in ['GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT']:
            for invalid in ['', '0', '-1', '+1', ' 1', '1.0', '1e3', '\u0661', True]:
                env = dict(self.fixture('push', 'environment'))
                env[field] = invalid
                with self.subTest(field=field, value=invalid), self.assertRaises(ValueError):
                    extract({}, env, '2026-10-25T00:00:00Z')


def object_paths(value, path=()):
    if isinstance(value, dict):
        yield path
        for key, child in value.items():
            yield from object_paths(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from object_paths(child, path + (index,))


def at_path(value, path):
    for key in path:
        value = value[key]
    return value


if __name__ == "__main__":
    unittest.main()
