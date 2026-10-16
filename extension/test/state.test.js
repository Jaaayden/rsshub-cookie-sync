import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_DEBOUNCE_MS,
  MAX_DEBOUNCE_WAIT_MS,
  createDefaultState,
  UPLOAD_REFRESH_MS,
  canSkipUpload,
  debounceFireAt,
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
    lastUploadAt: null,
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
    lastUploadAt: null,
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

test('a burst of cookie changes cannot postpone the debounced sync forever', () => {
  const start = 1_000_000;
  // The first change schedules the normal delay.
  assert.equal(debounceFireAt(start, start), start + DEFAULT_DEBOUNCE_MS);
  // Later changes keep pushing it back...
  const later = start + MAX_DEBOUNCE_WAIT_MS - 30_000;
  assert.equal(debounceFireAt(later, start), start + MAX_DEBOUNCE_WAIT_MS);
  // ...but never beyond the cap, and never into the past.
  const tooLate = start + MAX_DEBOUNCE_WAIT_MS + 5_000;
  assert.equal(debounceFireAt(tooLate, start), tooLate);
  // Without a recorded burst start the window starts now.
  assert.equal(debounceFireAt(start, undefined), start + DEFAULT_DEBOUNCE_MS);
});

test('an unchanged Cookie is only re-uploaded on start-up, manual sync or after the refresh interval', () => {
  const hash = 'c'.repeat(64);
  const now = 10 * UPLOAD_REFRESH_MS;
  let state = updateProviderState(createDefaultState(), 'zhihu', {
    hash,
    result: { status: 'unchanged', reason: 'direct_sync' },
    now: now - 1000,
  });
  const provider = state.providers.zhihu;
  assert.equal(provider.lastUploadAt, now - 1000);

  assert.equal(canSkipUpload(provider, hash, 'periodic', now), true);
  assert.equal(canSkipUpload(provider, hash, 'cookie_changed:zhihu', now), true);
  // Different Cookie, or a trigger that must always reach the server.
  assert.equal(canSkipUpload(provider, 'd'.repeat(64), 'periodic', now), false);
  for (const reason of ['startup', 'installed', 'enabled', 'manual']) {
    assert.equal(canSkipUpload(provider, hash, reason, now), false, reason);
  }
  // The confirmation is too old.
  assert.equal(canSkipUpload(provider, hash, 'periodic', now - 1000 + UPLOAD_REFRESH_MS), false);
  // A failed or unconfirmed last result is never skipped.
  for (const status of ['retryable_error', 'candidate_saved', 'rejected_invalid']) {
    state = updateProviderState(state, 'zhihu', { hash, result: { status }, now: now - 500 });
    assert.equal(canSkipUpload(state.providers.zhihu, hash, 'periodic', now), false, status);
  }
  // A check that fails does not advance the confirmation time.
  assert.equal(state.providers.zhihu.lastUploadAt, now - 1000);
});
