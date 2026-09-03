import test from 'node:test';
import assert from 'node:assert/strict';

import {
  HOST_RESULT_STATUSES,
  NATIVE_HOST_NAME,
  createSyncPayload,
  sanitizeHostResult,
} from '../lib/protocol.js';

test('native payload has a fixed version and only supported providers', () => {
  assert.deepEqual(
    createSyncPayload({ zhihu: 'z_c0=a=b', weibo: 'SUB=x; MLOGIN=y', ignored: 'not sent' }),
    {
      version: 1,
      providers: {
        zhihu: { cookieHeader: 'z_c0=a=b' },
        weibo: { cookieHeader: 'SUB=x; MLOGIN=y' },
      },
    },
  );
  assert.equal(NATIVE_HOST_NAME, 'com.jayden.rsshub_cookie_sync');
  assert.deepEqual(HOST_RESULT_STATUSES, [
    'unchanged',
    'candidate_saved',
    'promoted',
    'rejected_invalid',
    'retryable_error',
  ]);
});

test('payload rejects empty, unknown, malformed or unsupported input', () => {
  assert.throws(() => createSyncPayload({}), /at least one provider/);
  assert.throws(() => createSyncPayload({ zhihu: '' }), /must not be empty/);
  assert.throws(() => createSyncPayload({ tiktok: 'x=y' }), /at least one provider/);
  assert.throws(() => createSyncPayload({ zhihu: 'bad header' }), /without equals/);
});

test('native responses are reduced to a fixed vocabulary and never retain arbitrary text', () => {
  assert.deepEqual(
    sanitizeHostResult({ status: 'candidate_saved', reason: 'ok', cookieHeader: 'secret=must-not-copy' }, 'zhihu'),
    { status: 'candidate_saved', reason: 'ok' },
  );
  assert.deepEqual(
    sanitizeHostResult({ providers: { weibo: { status: 'promoted', reason: 'server_rejected', detail: 'secret' } } }, 'weibo'),
    { status: 'promoted', reason: 'server_rejected' },
  );
  assert.deepEqual(
    sanitizeHostResult({ status: 'unexpected', error: 'full response with cookie=secret' }, 'zhihu'),
    { status: 'retryable_error', reason: 'invalid_response' },
  );
  assert.deepEqual(
    sanitizeHostResult('secret cookie=foo', 'zhihu'),
    { status: 'retryable_error', reason: 'invalid_response' },
  );
});
