"""Synthetic Twitter credentials only; HTTP and Docker are injected fakes."""
import json
import unittest
import test_server
from test_server import QueueTransport, ScriptedProber
import rsshub_cookie_sync as sync

TOKEN = 'synthetic-twitter-token'
NEW = 'synthetic-new-token'
CSRF = 'synthetic-csrf'
OK = sync.ProbeResult('ok', 200, 'profile_ok')
BAD = sync.ProbeResult('auth_failed', 401, 'http_401')
TEMP = sync.ProbeResult('transient', 429, 'http_429')


def homepage(*cookies):
    return sync.HTTPResponse(200, b'', cookies or ('ct0=' + CSRF + '; Domain=.x.com; Path=/; Secure',))


class TwitterProbeTests(unittest.TestCase):
    def probe(self, responses, token=TOKEN):
        transport = QueueTransport(responses)
        result = sync.ProviderProber(sync.RuntimeConfig(), transport).probe('twitter', token)
        return result, transport

    def test_account_success_and_exact_credential_destinations(self):
        result, transport = self.probe([homepage(), sync.HTTPResponse(200, b'{"screen_name":"test_user"}')])
        self.assertTrue(result.ok)
        first, second = transport.requests
        self.assertEqual(first[1], 'https://x.com/')
        self.assertEqual(first[2]['Cookie'], 'auth_token=' + TOKEN)
        self.assertEqual(second[1], 'https://api.x.com/1.1/account/settings.json')
        self.assertEqual(second[2]['x-csrf-token'], CSRF)
        self.assertEqual(second[2]['Cookie'], 'auth_token=' + TOKEN + '; ct0=' + CSRF)
        self.assertEqual(second[2]['x-twitter-auth-type'], 'OAuth2Session')

    def test_unusable_csrf_never_sends_account_request(self):
        for cookies in [(), ('ct0=bad; Domain=evil.test; Path=/',),
                        ('ct0=a; Path=/', 'ct0=b; Path=/'), ('ct0=a; Path=/elsewhere',), ('other=a',)]:
            with self.subTest(cookies=cookies):
                result, transport = self.probe([sync.HTTPResponse(200, b'', cookies)])
                self.assertEqual(result.kind, 'transient')
                self.assertEqual(len(transport.requests), 1)

    def test_failures_are_classified_without_promoting_challenge_pages(self):
        for status in [403, 404, 429, 432, 500, 503, 302]:
            with self.subTest(status=status):
                result, _ = self.probe([homepage(), sync.HTTPResponse(status, b'{}')])
                self.assertEqual(result.kind, 'transient')
        for body in [b'{}', b'<html>login</html>', b'{"screen_name":true}',
                     b'{"screen_name":"bad name"}', b'{"errors":[{"code":239}]}',
                     b'{"screen_name":"tester","errors":[{"code":239}]}']:
            result, _ = self.probe([homepage(), sync.HTTPResponse(200, body)])
            self.assertEqual(result.kind, 'transient')
        for response in [sync.HTTPResponse(401, b'{}'), sync.HTTPResponse(200, b'{"errors":[{"code":89}]}')]:
            result, _ = self.probe([homepage(), response])
            self.assertEqual(result.kind, 'auth_failed')
        for responses in [[sync.ProbeError('synthetic')], [homepage(), sync.ProbeError('synthetic')]]:
            result, _ = self.probe(responses)
            self.assertEqual(result.kind, 'transient')

    def test_malformed_tokens_never_reach_transport(self):
        for token in ['', 'a,b', 'auth_token=a', 'a; ct0=b', 'a\n', 'a b', 'x' * 4086]:
            result, transport = self.probe([], token)
            self.assertEqual(result.kind, 'auth_failed')
            self.assertEqual(transport.requests, [])


class TwitterServiceTests(unittest.TestCase):
    setUp = test_server.ServerTests.setUp
    tearDown = test_server.ServerTests.tearDown

    def apply(self, token=TOKEN):
        return self.service.apply(sync.build_manual_update_request('twitter', token))

    def isolate(self, token=''):
        sync.atomic_write(self.live, ('TWITTER_AUTH_TOKEN=' + token + '\n').encode() if token else b'')
        self.service.prober = ScriptedProber([])

    def test_manual_and_wire_format(self):
        expected = {'version': 1, 'providers': {'twitter': {'cookieHeader': 'auth_token=' + TOKEN}}}
        self.assertEqual(sync.build_manual_update_request('twitter', TOKEN), expected)
        self.assertEqual(sync.build_manual_update_request('twitter', 'auth_token=' + TOKEN), expected)
        self.assertEqual(self.service.apply({'version': 1, 'providers': {'twitter': {'cookieHeader': TOKEN}}}), {'status': 'rejected_invalid'})
        for value in ['a,b', 'auth_token=a; ct0=b', 'auth_token=a; auth_token=b']:
            with self.assertRaises(sync.InvalidInput):
                sync.build_manual_update_request('twitter', value)

    def test_first_sync_writes_only_raw_token_and_unchanged_does_not_recreate(self):
        self.isolate()
        self.assertEqual(self.apply(), {'status': 'promoted'})
        self.assertEqual(self.live.read_text(), 'TWITTER_AUTH_TOKEN=' + TOKEN + '\n')
        calls = len(self.docker.calls)
        self.assertEqual(self.apply(), {'status': 'unchanged'})
        self.assertEqual(len(self.docker.calls), calls)
        state = sync.load_state(self.config.state_file)
        self.assertEqual(state['providers']['twitter']['live_hash'], sync.sha256_prefix(TOKEN))
        self.assertNotIn(TOKEN, self.config.state_file.read_text())
        self.assertEqual(state['bootstrap']['status'], 'seeded')

    def test_candidate_switches_after_two_confirmed_failures(self):
        self.isolate(TOKEN)
        self.assertEqual(self.apply(NEW), {'status': 'candidate_saved'})
        self.assertEqual(self.service._read_candidate('twitter'), NEW)
        # Missing legacy values do not consume the scripted Twitter probe queue.
        self.service.prober = ScriptedProber([BAD])
        self.service.monitor()
        self.assertIn(TOKEN, self.live.read_text())
        self.service.prober = ScriptedProber([BAD, OK, OK])
        self.service.monitor()
        self.assertEqual(self.live.read_text(), 'TWITTER_AUTH_TOKEN=' + NEW + '\n')
        self.assertIsNone(self.service._read_candidate('twitter'))

    def test_temporary_probe_preserves_live_and_candidate(self):
        self.isolate(TOKEN)
        self.apply(NEW)
        self.service.prober = ScriptedProber([TEMP])
        self.assertEqual(self.apply('another-token'), {'status': 'retryable_error'})
        self.assertEqual(self.service._read_candidate('twitter'), NEW)
        self.service.prober = ScriptedProber([TEMP])
        self.service.monitor()
        self.assertIn(TOKEN, self.live.read_text())
        self.assertEqual(sync.load_state(self.config.state_file)['providers']['twitter']['auth_failures'], 0)

    def test_post_recreate_auth_failure_rolls_back(self):
        self.isolate()
        self.service.prober = ScriptedProber([OK, BAD])
        with self.assertRaises(sync.TransactionError):
            self.apply()
        self.assertEqual(self.live.read_bytes(), b'')

    def test_unconfigured_twitter_never_probes_or_notifies(self):
        sync.atomic_write(self.live, b'ZHIHU_COOKIES=z_c0=a\nWEIBO_COOKIES=SUB=b\n')
        self.service.prober = ScriptedProber([])
        self.service.monitor()
        self.service.monitor()
        self.assertFalse(any(p == 'twitter' for p, _, _ in self.service.prober.calls))
        self.assertNotIn('twitter', json.dumps(self.notifier.events))
        state = sync.load_state(self.config.state_file)
        self.assertEqual(state['providers']['twitter']['last_error'], 'twitter_not_configured')
        self.assertEqual(state['bootstrap']['status'], 'seeded')

    def test_pool_is_preserved_and_reported_without_probe(self):
        self.isolate(TOKEN + ',' + NEW)
        before = self.live.read_bytes()
        self.assertEqual(self.apply('third-token'), {'status': 'rejected_invalid'})
        self.service.monitor()
        self.assertEqual(self.live.read_bytes(), before)
        self.assertEqual(self.service.prober.calls, [])
        self.assertEqual(self.service.public_status()['providers']['twitter']['last_error'], 'twitter_token_pool_unsupported')
        self.assertIsNone(self.service._read_candidate('twitter'))

    def test_partial_bootstrap_and_old_state_upgrade(self):
        self.isolate(TOKEN)
        self.compose.write_text('services:\n  rsshub:\n    image: example\n    env_file:\n      - path: ./secrets/rsshub.env\n        format: raw\n')
        self.assertEqual(self.service.bootstrap(), {'version': 1, 'bootstrapped': True})
        self.assertEqual(self.service.prober.calls, [('twitter', TOKEN, True)])
        self.assertEqual(sync.load_state(self.config.state_file)['bootstrap']['status'], 'seeded')
        self.config.state_file.write_text('{"version":1,"providers":{"weibo":{"last_probe":"ok"}}}')
        state = sync.load_state(self.config.state_file)
        self.assertIn('twitter', state['providers'])
        self.assertEqual(state['providers']['weibo']['last_probe'], 'ok')

    def test_migration_retains_pool_verbatim(self):
        pool = TOKEN + ',' + NEW
        rendered = sync.render_env(b'OTHER=keep\n', {'TWITTER_AUTH_TOKEN': pool})
        self.assertEqual(rendered, ('OTHER=keep\nTWITTER_AUTH_TOKEN=' + pool + '\n').encode())

    def test_compose_migration_preserves_optional_empty_and_pool_values(self):
        for value in ['', TOKEN + ', ' + NEW]:
            with self.subTest(value=value):
                self.compose.write_text('services:\n  rsshub:\n    image: example\n    environment:\n      TWITTER_AUTH_TOKEN: "' + value + '"\n')
                sync.atomic_write(self.live, b'')
                sync.migrate_compose_file(self.compose, self.live)
                self.assertEqual(sync.parse_env(self.live.read_bytes())['TWITTER_AUTH_TOKEN'], value)
                sync.finalize_migration(self.compose, self.live)

    def test_pool_does_not_block_an_independent_weibo_update(self):
        self.isolate(TOKEN + ',' + NEW)
        result = self.service.apply({'version': 1, 'providers': {
            'twitter': {'cookieHeader': 'auth_token=third-token'},
            'weibo': {'cookieHeader': 'SUB=synthetic-weibo'},
        }})
        self.assertEqual(result, {'status': 'promoted'})
        values = sync.parse_env(self.live.read_bytes())
        self.assertEqual(values['TWITTER_AUTH_TOKEN'], TOKEN + ',' + NEW)
        self.assertEqual(values['WEIBO_COOKIES'], 'SUB=synthetic-weibo')
        self.assertFalse(any(provider == 'twitter' for provider, _, _ in self.service.prober.calls))
