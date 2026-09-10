import test from 'node:test';
import assert from 'node:assert/strict';
import { twitterCookieHeader, applicableCookies } from '../lib/cookies.js';
import { createSyncPayload } from '../lib/protocol.js';
import { sanitizeState } from '../lib/state.js';
import { createCookieCopyActions } from '../lib/popup-actions.js';
const token = 'synthetic-twitter-token';
const cookie = (value = token, domain = '.x.com') => ({ name: 'auth_token', value, domain, path: '/', secure: true });

test('Twitter extraction selects only auth_token and rejects ambiguity', () => {
  assert.equal(twitterCookieHeader([cookie(), cookie(), { ...cookie(), name: 'ct0' }], 'x.com'), `auth_token=${token}`);
  assert.equal(twitterCookieHeader([cookie(token, '.twitter.com')], 'x.com'), null);
  assert.equal(twitterCookieHeader([cookie(token, '.evilx.com'), { ...cookie(), path: '/private' }, { ...cookie(), partitionKey: {} }, { ...cookie(), expirationDate: 1 }], 'x.com'), null);
  for (const value of [undefined, null, 123, '', 'a,b', 'a;ct0=b', 'a\n', 'x'.repeat(4086)]) {
    assert.throws(() => twitterCookieHeader([{ ...cookie(), value }], 'x.com'));
  }
  assert.throws(() => twitterCookieHeader([cookie('one'), cookie('two')], 'x.com'));
  assert.equal(applicableCookies('twitter', [cookie(token, '.twitter.com')]).length, 1);
  assert.equal(applicableCookies('twitter', [{ ...cookie(), name: 'ct0' }]).length, 0);
});

test('Twitter uses v1 canonical header and older persisted state gets metadata only', () => {
  assert.deepEqual(createSyncPayload({ twitter: `auth_token=${token}` }), { version: 1, providers: { twitter: { cookieHeader: `auth_token=${token}` } } });
  for (const value of [token, 'auth_token=a; ct0=b', 'auth_token=a; auth_token=b']) {
    assert.throws(() => createSyncPayload({ twitter: value }));
  }
  const state = sanitizeState({ providers: { weibo: { lastResult: 'unchanged' }, twitter: { cookieHeader: token } } });
  assert.equal(state.providers.weibo.lastResult, 'unchanged');
  assert.ok(state.providers.twitter);
  assert.equal(JSON.stringify(state).includes(token), false);
});

test('Twitter copy confirms and authorizes before reading, then copies only bare token', async () => {
  const calls = [];
  const actions = createCookieCopyActions({
    confirmCopy: () => { calls.push('confirm'); return true; },
    requestClipboardPermission: async () => { calls.push('permission'); return true; },
    sendMessage: async (message) => { calls.push(message); return { ok: true, provider: 'twitter', cookieHeader: `auth_token=${token}` }; },
    writeClipboard: async (value) => calls.push(value),
    showNotice: () => {},
  });
  const button = { textContent: '复制 Auth Token', disabled: false };
  assert.equal(await actions.copyProviderCookie('twitter', button), true);
  assert.deepEqual(calls, ['confirm', 'permission', { type: 'copy-cookie', provider: 'twitter' }, token]);
  assert.equal(button.textContent, '复制 Auth Token');
  assert.equal(button.disabled, false);
});
