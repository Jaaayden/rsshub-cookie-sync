import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_DEBOUNCE_MS,
  createDefaultState,
  nextDebounceAt,
  parseDebounceAlarm,
  sanitizeState,
  updateProviderState,
} from '../lib/state.js';

test('default and persisted state contain metadata only', () => {
  const state = createDefaultState();
  assert.equal(state.enabled, true);
  assert.equal(state.providers.zhihu.hash, null);
  assert.equal(Object.hasOwn(state.providers.zhihu, 'cookieHeader'), false);

  const sanitized = sanitizeState({
    enabled: false,
    providers: {
      zhihu: {
        hash: 'a'.repeat(64),
        lastSyncAt: 123,
        lastResult: 'promoted',
        lastReason: 'ok',
        cookieHeader: 'secret=never-persist',
      },
      weibo: { hash: 'not-a-hash', lastReason: 'secret=leak' },
    },
  });
  assert.equal(sanitized.enabled, false);
  assert.deepEqual(sanitized.providers.zhihu, {
    hash: 'a'.repeat(64),
    lastSyncAt: 123,
    lastResult: 'promoted',
    lastReason: 'ok',
  });
  assert.equal(sanitized.providers.weibo.hash, null);
  assert.equal(sanitized.providers.weibo.lastReason, null);
  assert.equal(JSON.stringify(sanitized).includes('secret'), false);
});

test('provider updates persist hash and safe status but never a Cookie header', () => {
  const state = updateProviderState(createDefaultState(), 'zhihu', {
    hash: 'b'.repeat(64),
    result: { status: 'candidate_saved', reason: 'ok' },
    now: 1700000000000,
    cookieHeader: 'z_c0=must-not-persist',
  });
  assert.deepEqual(state.providers.zhihu, {
    hash: 'b'.repeat(64),
    lastSyncAt: 1700000000000,
    lastResult: 'candidate_saved',
    lastReason: 'ok',
  });
  assert.equal(JSON.stringify(state).includes('must-not-persist'), false);
});

test('debounce deadline and alarm parsing are deterministic', () => {
  assert.equal(nextDebounceAt(1000), 1000 + DEFAULT_DEBOUNCE_MS);
  assert.equal(nextDebounceAt(1000, 0), 1000);
  assert.equal(parseDebounceAlarm('rsshub-cookie-sync:debounce:zhihu'), 'zhihu');
  assert.equal(parseDebounceAlarm('rsshub-cookie-sync:debounce:weibo'), 'weibo');
  assert.equal(parseDebounceAlarm('rsshub-cookie-sync:debounce:tiktok'), null);
  assert.equal(parseDebounceAlarm('periodic'), null);
  assert.throws(() => nextDebounceAt(Number.NaN), /finite/);
});
