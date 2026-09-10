import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

import test_server
import rsshub_cookie_sync as sync


class DirectSyncTests(unittest.TestCase):
    tearDown = test_server.ServerTests.tearDown

    def setUp(self):
        test_server.ServerTests.setUp(self)
        self.config = replace(self.config, sync_mode='direct')
        self.service = sync.SyncService(self.config, transport=self.transport, runner=self.docker,
                                       notifier=self.notifier, clock=self.clock)
        self.service.prober = Mock()
        self.service.prober.probe.side_effect = AssertionError('direct mode must never call upstream')

    def request(self, **providers):
        return {'version': 1, 'diagnostics': True,
                'providers': {name: {'cookieHeader': value} for name, value in providers.items()}}

    def test_direct_updates_both_blocked_providers_without_upstream(self):
        result = self.service.apply(self.request(zhihu='z_c0=synthetic-direct', twitter='auth_token=synthetic-direct'))
        self.assertEqual(result, {'status': 'promoted', 'reason': 'direct_sync'})
        values = sync.parse_env(self.live.read_bytes())
        self.assertEqual(values['ZHIHU_COOKIES'], 'z_c0=synthetic-direct')
        self.assertEqual(values['TWITTER_AUTH_TOKEN'], 'synthetic-direct')
        self.assertEqual(values['WEIBO_COOKIES'], test_server.WB_OLD)
        self.assertEqual(self.docker.calls.count(('recreate',)), 1)
        self.service.prober.probe.assert_not_called()
        state = sync.load_state(self.config.state_file)
        for provider in ('zhihu', 'twitter'):
            self.assertEqual(state['providers'][provider]['last_probe'], 'unknown')
            self.assertIsNone(state['providers'][provider]['last_success_at'])
        self.assertTrue(all('未验证上游' in body for _, body in self.notifier.events))

    def test_unchanged_removes_stale_candidate_without_recreate(self):
        self.service._save_candidate('zhihu', 'z_c0=stale')
        result = self.service.apply(self.request(zhihu=test_server.ZH_OLD))
        self.assertEqual(result, {'status': 'unchanged', 'reason': 'direct_sync'})
        self.assertEqual(self.docker.calls, [])
        self.assertIsNone(self.service._read_candidate('zhihu'))
        self.service.prober.probe.assert_not_called()

    def test_health_failure_restores_exact_live_bytes(self):
        old = self.live.read_bytes()
        self.service.docker.wait_healthy = Mock(side_effect=[False, True])
        with self.assertRaises(sync.TransactionError):
            self.service.apply(self.request(twitter='auth_token=synthetic-new'))
        self.assertEqual(self.live.read_bytes(), old)
        self.service.prober.probe.assert_not_called()

    def test_direct_monitor_does_not_rotate_old_candidates_or_emit_auth_alerts(self):
        self.service._save_candidate('zhihu', 'z_c0=stale')
        state = sync.load_state(self.config.state_file)
        state['providers']['zhihu'].update(auth_failures=5, transient_failures=8, last_probe='auth_failed',
                                          last_error='moments_invalid_json', candidate_validation='ok')
        sync.save_state(self.config.state_file, state)
        old = self.live.read_bytes()
        for _ in range(4):
            self.service.monitor()
        self.assertEqual(self.live.read_bytes(), old)
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.notifier.events, [])
        self.service.prober.probe.assert_not_called()
        state = sync.load_state(self.config.state_file)
        self.assertEqual(state['providers']['zhihu']['auth_failures'], 0)
        self.assertEqual(state['providers']['zhihu']['transient_failures'], 0)
        self.assertIsNone(state['providers']['zhihu']['last_error'])
        self.assertEqual(self.service.public_status()['sync_mode'], 'direct')

    def test_direct_monitor_still_alerts_on_rsshub_failure(self):
        # _health_probe uses the injected transport; return an unhealthy response.
        self.transport.request = Mock(return_value=sync.HTTPResponse(503, b''))
        self.service.monitor()
        self.assertTrue(any('健康检查失败' in title for title, _ in self.notifier.events))
        self.service.prober.probe.assert_not_called()

    def test_format_checks_and_pool_protection_still_apply(self):
        old = self.live.read_bytes()
        result = self.service.apply(self.request(twitter='not-a-cookie'))
        self.assertEqual(result['status'], 'rejected_invalid')
        self.assertEqual(self.live.read_bytes(), old)
        sync.atomic_write(self.live, b'TWITTER_AUTH_TOKEN=one,two\n')
        result = self.service.apply(self.request(twitter='auth_token=three'))
        self.assertEqual(result, {'status': 'rejected_invalid', 'reason': 'twitter_token_pool_unsupported'})
        self.assertEqual(self.live.read_bytes(), b'TWITTER_AUTH_TOKEN=one,two\n')
        self.assertEqual(self.docker.calls, [])

    def test_old_config_defaults_direct_and_verified_is_explicit(self):
        for mode in (None, 'verified', 'direct'):
            data = {} if mode is None else {'sync_mode': mode}
            sync.atomic_write(self.config.config_file, json.dumps(data).encode())
            loaded = sync.RuntimeConfig.from_file(self.config.config_file)
            self.assertEqual(loaded.sync_mode, mode or 'direct')
        with self.assertRaises(sync.SyncError):
            replace(self.config, sync_mode='invalid').validate()

    def test_bootstrap_skips_upstream_but_keeps_health_checks(self):
        self.compose.write_text('services:\n  rsshub:\n    image: example\n    env_file:\n      - path: ./secrets/rsshub.env\n        format: raw\n')
        self.service.docker.wait_healthy = Mock(return_value=True)
        self.service.bootstrap()
        self.service.prober.probe.assert_not_called()
        self.service.docker.wait_healthy.assert_called_once()
        self.assertEqual(sync.load_state(self.config.state_file)['providers']['zhihu']['last_probe'], 'unknown')

    def test_route_failure_cooldown_recovery_and_no_credential_rotation(self):
        self.config.rsshub_routes['zhihu'] = '/zhihu/people/activities/example'
        old = self.live.read_bytes()
        self.service._health_probe = Mock(return_value=sync.ProbeResult('ok', 200, 'ok'))
        self.transport.request = Mock(return_value=sync.HTTPResponse(503, b'private body'))
        self.service.monitor()
        self.assertEqual(self.notifier.events, [])
        for _ in range(3):
            status = self.service.monitor()
        self.assertEqual(len(self.notifier.events), 1)
        self.assertEqual(status['providers']['zhihu']['route_failures'], 4)
        self.assertNotIn('private body', json.dumps(status))
        self.transport.request.return_value = sync.HTTPResponse(200, b'<rss><channel/></rss>')
        status = self.service.monitor()
        self.assertEqual(status['providers']['zhihu']['route_probe'], 'ok')
        self.assertEqual(status['providers']['zhihu']['last_probe'], 'unknown')
        self.assertEqual(len(self.notifier.events), 2)
        self.assertEqual(old, self.live.read_bytes())
        self.assertEqual(self.docker.calls, [])
        self.assertNotIn('headers', self.transport.request.call_args.kwargs)
        self.service.prober.probe.assert_not_called()

    def test_route_response_classification(self):
        self.config.rsshub_routes['zhihu'] = '/zhihu/people/activities/example'
        self.service._health_probe = Mock(return_value=sync.ProbeResult('ok', 200, 'ok'))
        cases = [
            (200, b'<feed xmlns="http://www.w3.org/2005/Atom"/>', None),
            (200, b'<rss><channel/></rss>', None),
            (200, b'<?xml version="1.0" encoding="not-real"?><rss><channel/></rss>', 'route_invalid_feed'),
            (200, b'<html>login</html>', 'route_invalid_feed'),
            (200, b'{"error":"private"}', 'route_invalid_feed'),
            (200, b'<!DOCTYPE rss [<!ENTITY x "x">]><rss><channel/></rss>', 'route_invalid_feed'),
            (200, '<!DOCTYPE rss><rss><channel/></rss>'.encode('utf-16'), 'route_invalid_feed'),
            (200, b'x' * (sync.MAX_FEED_BYTES + 1), 'route_too_large'),
            (302, b'', 'route_http_302'), (401, b'', 'route_http_401'),
            (404, b'', 'route_http_404'), (429, b'', 'route_http_429'),
        ]
        for code, body, error in cases:
            with self.subTest(code=code, error=error):
                self.transport.request = Mock(return_value=sync.HTTPResponse(code, body))
                result = self.service.monitor()['providers']['zhihu']
                self.assertEqual(result['route_error'], error)
                self.assertEqual(result['auth_failures'], 0)
        self.transport.request = Mock(side_effect=sync.ProbeError('private network details'))
        self.assertEqual(self.service.monitor()['providers']['zhihu']['route_error'], 'route_network_error')

    def test_route_skipped_without_live_or_when_service_unhealthy(self):
        sync.atomic_write(self.live, b'ZHIHU_COOKIES=z_c0=synthetic\n')
        self.config.rsshub_routes['twitter'] = '/twitter/user/example'
        self.service._health_probe = Mock(return_value=sync.ProbeResult('ok', 200, 'ok'))
        self.transport.request = Mock(side_effect=AssertionError('must skip'))
        self.service.monitor()
        self.config.rsshub_routes['zhihu'] = '/zhihu/people/activities/example'
        self.service._health_probe.return_value = sync.ProbeResult('transient', 503, 'rsshub_health_http_503')
        self.service.monitor()
        self.transport.request.assert_not_called()

    def test_route_config_validation_and_old_state_migration(self):
        for route in ('https://example.com/zhihu/a', '//example.com', '/weibo/user/a',
                      '/zhihu/../healthz', '/zhihu/a?key=private', '/zhihu/a#x', '/zhihu/a\n'):
            with self.subTest(route=route), self.assertRaises(sync.SyncError):
                replace(self.config, rsshub_routes={'zhihu': route}).validate()
        data = {'providers': {'zhihu': {'rsshub_route': '/zhihu/people/activities/example'}}}
        sync.atomic_write(self.config.config_file, json.dumps(data).encode())
        loaded = sync.RuntimeConfig.from_file(self.config.config_file)
        loaded.validate()
        self.assertEqual(loaded.rsshub_routes, {'zhihu': '/zhihu/people/activities/example'})
        merged = sync._merge_state({'version': 1, 'providers': {'zhihu': {'last_probe': 'ok'}}})
        self.assertEqual(merged['providers']['zhihu']['route_probe'], 'unknown')
