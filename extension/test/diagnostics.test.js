import test from 'node:test';
import assert from 'node:assert/strict';
import { sanitizeDiagnosticLog, sanitizeHostResult } from '../lib/protocol.js';
import { syncSummary } from '../lib/popup-actions.js';

test('diagnostic schema bounds retained entries and strips arbitrary secrets', () => {
  const row = { time: 100, provider: 'twitter', stage: 'complete', status: 'retryable_error', reason: 'http_403', cookie: 'SECRET', stderr: 'SECRET', url: 'SECRET', durationMs: 15 };
  const logs = sanitizeDiagnosticLog(Array.from({ length: 220 }, () => row));
  assert.equal(logs.length, 200);
  assert.equal(logs[0].reason, 'http_403');
  assert.equal(JSON.stringify(logs).includes('SECRET'), false);
  assert.equal(sanitizeDiagnosticLog([{ ...row, reason: 'SECRET' }])[0].reason, null);
  assert.deepEqual(sanitizeDiagnosticLog([{ ...row, provider: 'SECRET' }, { ...row, stage: 'SECRET' }]), []);
  assert.equal(sanitizeHostResult({ status: 'retryable_error', reason: 'ssh_auth_failed' }).reason, 'ssh_auth_failed');
  assert.equal(sanitizeHostResult({ status: 'retryable_error', reason: 'SECRET' }).reason, undefined);
});

test('summary distinguishes partial success and never diagnoses SSH from generic retry', () => {
  const providers = { zhihu: { lastResult: 'retryable_error' }, weibo: { lastResult: 'unchanged' }, twitter: { lastResult: 'retryable_error' } };
  assert.match(syncSummary(providers).text, /部分完成：1\/3/);
  assert.doesNotMatch(syncSummary(providers).text, /SSH|公钥/);
  assert.equal(syncSummary(providers, ['weibo']).kind, 'success');
  assert.equal(syncSummary(providers, ['twitter']).kind, 'error');
  assert.equal(syncSummary({}).kind, 'error');
});
