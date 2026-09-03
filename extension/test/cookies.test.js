import test from 'node:test';
import assert from 'node:assert/strict';

import {
  COOKIE_TARGETS,
  applicableCookies,
  cookieAppliesToTarget,
  fingerprint,
  isCookieTargetUrl,
  serializeCookieHeader,
  sha256Hex,
  validateCookieHeader,
} from '../lib/cookies.js';

function cookie(overrides = {}) {
  return {
    name: 'sid',
    value: 'abc',
    domain: 'www.zhihu.com',
    path: '/',
    secure: true,
    hostOnly: true,
    storeId: '0',
    ...overrides,
  };
}

test('target URL matching is exact by scheme, host, port and path', () => {
  assert.equal(isCookieTargetUrl('zhihu', COOKIE_TARGETS.zhihu.url), true);
  assert.equal(isCookieTargetUrl('zhihu', `${COOKIE_TARGETS.zhihu.url}?limit=1`), true);
  assert.equal(isCookieTargetUrl('zhihu', 'https://www.zhihu.com/api/v4/me'), false);
  assert.equal(isCookieTargetUrl('zhihu', 'http://www.zhihu.com/api/v3/moments'), false);
  assert.equal(isCookieTargetUrl('zhihu', 'https://zhihu.com/api/v3/moments'), false);
  assert.equal(isCookieTargetUrl('weibo', 'https://m.weibo.cn/feed/group'), true);
  assert.equal(isCookieTargetUrl('weibo', 'https://m.weibo.cn:4443/feed/group'), false);
});

test('cookie matching handles host-only, domain, path and secure attributes', () => {
  assert.equal(cookieAppliesToTarget('zhihu', cookie()), true);
  assert.equal(cookieAppliesToTarget('zhihu', cookie({ domain: '.zhihu.com', hostOnly: false })), true);
  assert.equal(cookieAppliesToTarget('zhihu', cookie({ domain: 'evilzhihu.com', hostOnly: false })), false);
  assert.equal(cookieAppliesToTarget('zhihu', cookie({ domain: 'zhihu.com', hostOnly: true })), false);
  assert.equal(cookieAppliesToTarget('zhihu', cookie({ path: '/api/v3' })), true);
  assert.equal(cookieAppliesToTarget('zhihu', cookie({ path: '/api/v4' })), false);
  assert.equal(cookieAppliesToTarget('zhihu', cookie({ path: '/api/v3/moments-more' })), false);
  assert.equal(cookieAppliesToTarget('zhihu', cookie({ secure: false })), true);
  assert.equal(cookieAppliesToTarget('weibo', cookie({ domain: 'm.weibo.cn', hostOnly: true })), true);
});

test('applicableCookies filters records for the endpoint', () => {
  const records = [
    cookie({ name: 'keep', value: '1' }),
    cookie({ name: 'wrong-path', path: '/api/v4' }),
    cookie({ name: 'wrong-host', domain: 'example.com', hostOnly: true }),
    { name: 'not-a-cookie' },
  ];
  assert.deepEqual(
    applicableCookies('zhihu', records).map((item) => item.name),
    ['keep'],
  );
});

test('serialization is stable, preserves equals and retains distinct duplicate names', () => {
  const records = [
    cookie({ name: 'z_c0', value: 'token=a=b', domain: '.zhihu.com', hostOnly: false }),
    cookie({ name: 'd_c0', value: 'x%3Dy' }),
    cookie({ name: 'z_c0', value: 'token=a=b', domain: '.zhihu.com', hostOnly: false }),
    cookie({ name: 'z_c0', value: 'path-token', path: '/api/v3' }),
  ];
  const shuffled = [records[2], records[0], records[3], records[1]];
  const expected = 'z_c0=path-token; d_c0=x%3Dy; z_c0=token=a=b';
  assert.equal(serializeCookieHeader(records), expected);
  assert.equal(serializeCookieHeader(shuffled), expected);
  assert.equal(serializeCookieHeader([]), '');
});

test('malformed cookie parts and injected controls are rejected', () => {
  assert.throws(() => serializeCookieHeader([cookie({ name: 'bad name' })]), /invalid character/);
  assert.throws(() => serializeCookieHeader([cookie({ value: 'one; two' })]), /invalid character/);
  assert.throws(() => serializeCookieHeader([cookie({ value: 'one\r\ntwo' })]), /invalid character/);
  assert.throws(() => validateCookieHeader('a=1; injected'), /without equals/);
  assert.throws(() => validateCookieHeader('a=1\nX: secret'), /control character/);
  assert.throws(() => validateCookieHeader('', undefined), /must not be empty/);
});

test('SHA-256 fingerprints are deterministic and only expose a short prefix', async () => {
  assert.equal(
    await sha256Hex('zhihu=a; weibo=b'),
    '5811c5778b46d2a52916193aa492adf480cd25593770a3467b5d3df909d87796',
  );
  assert.equal(fingerprint('5811c5778b46d2a52916193aa492adf480cd25593770a3467b5d3df909d87796'), '5811c5778b46');
  assert.equal(fingerprint(''), null);
});
