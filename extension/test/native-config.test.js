import test from 'node:test';
import assert from 'node:assert/strict';

import {
  NATIVE_CONFIG_DEFAULTS,
  createGetNativeConfigMessage,
  createSetNativeConfigMessage,
  identityLabel,
  nativeConfigErrorMessage,
  normalizeNativeConfigInput,
  sanitizeNativeConfigResponse,
} from '../lib/native-config.js';

const valid = {
  host: 'rsshub.example.test',
  port: 2222,
  user: 'rsshub-sync',
  identityName: 'rsshub-cookie-sync',
};

test('连接设置使用安全默认值，get 请求不包含任何 Cookie 字段', () => {
  assert.deepEqual(NATIVE_CONFIG_DEFAULTS, {
    host: '',
    port: 22,
    user: 'rsshub-sync',
    identityName: 'rsshub-cookie-sync',
  });
  assert.deepEqual(createGetNativeConfigMessage(), {
    version: 1,
    action: 'get-config',
  });
  assert.equal(JSON.stringify(createGetNativeConfigMessage()).includes('cookie'), false);
});

test('表单值会规范化为固定的 host/port/user/identityName 控制消息', () => {
  assert.deepEqual(
    normalizeNativeConfigInput({
      host: '  rsshub.example.test ',
      port: '2222',
      user: ' rsshub-sync ',
      identityName: ' rsshub-cookie-sync ',
    }),
    valid,
  );
  assert.deepEqual(createSetNativeConfigMessage(valid), {
    version: 1,
    action: 'set-config',
    server: {
      host: valid.host,
      port: valid.port,
      user: valid.user,
    },
    identityName: valid.identityName,
  });
});

test('连接设置拒绝注入、路径和无效端口', () => {
  for (const candidate of [
    { ...valid, host: 'example.test;curl evil' },
    { ...valid, host: 'example.test\nssh' },
    { ...valid, user: 'root' },
    { ...valid, user: 'other-user' },
    { ...valid, user: 'root --proxy-command=bad' },
    { ...valid, identityName: '../id_ed25519' },
    { ...valid, identityName: 'key name' },
    { ...valid, port: 0 },
    { ...valid, port: 65536 },
    { ...valid, port: 22.5 },
  ]) {
    assert.equal(normalizeNativeConfigInput(candidate), null);
    assert.throws(() => createSetNativeConfigMessage(candidate), /invalid native configuration/);
  }
  assert.equal(normalizeNativeConfigInput({ ...valid, host: '' }), null);
  assert.deepEqual(
    normalizeNativeConfigInput({ ...NATIVE_CONFIG_DEFAULTS }, { allowEmptyHost: true }),
    NATIVE_CONFIG_DEFAULTS,
  );
  assert.deepEqual(
    sanitizeNativeConfigResponse({
      status: 'config',
      server: { host: valid.host, port: 22, user: 'root' },
      identityName: valid.identityName,
      identities: [{ name: valid.identityName, legacy: false }],
    }),
    { ok: false, error: 'invalid_response' },
  );
});

test('Native Host 响应只保留脱敏配置、身份名和标准指纹', () => {
  const response = sanitizeNativeConfigResponse({
    status: 'config',
    server: {
      host: valid.host,
      port: valid.port,
      user: valid.user,
    },
    identityName: valid.identityName,
    identities: [
      { name: 'rsshub-cookie-sync', legacy: false },
      { name: '../secret', legacy: false },
      { name: 'other-key', legacy: false },
      { name: 'other-key', legacy: true },
    ],
    cookieHeader: 'must-never-reach-options',
    privateKey: 'must-never-reach-options',
  });
  assert.deepEqual(response, {
    ok: true,
    config: valid,
    identities: [
      { name: 'rsshub-cookie-sync', legacy: false },
      { name: 'other-key', legacy: false },
    ],
  });
  assert.equal(JSON.stringify(response).includes('must-never-reach-options'), false);
});

test('异常 Native 响应只能转换为固定错误码', () => {
  assert.deepEqual(
    sanitizeNativeConfigResponse({ ok: false, error: 'server said cookie=secret' }),
    { ok: false, error: 'invalid_response' },
  );
  assert.deepEqual(
    sanitizeNativeConfigResponse({ ok: false, error: 'configuration_missing', detail: 'secret' }),
    { ok: false, error: 'configuration_missing' },
  );
  assert.deepEqual(
    sanitizeNativeConfigResponse({
      ok: true,
      server: { host: 'x', port: 22, user: 'rsshub-sync' },
      identityName: null,
    }),
    { ok: false, error: 'invalid_response' },
  );
  assert.equal(nativeConfigErrorMessage('configuration_missing'), '尚未配置连接信息。');
  assert.equal(nativeConfigErrorMessage('arbitrary secret'), 'Native Host 返回了无效状态。');
  assert.equal(identityLabel({ name: 'rsshub-cookie-sync', legacy: false }), 'rsshub-cookie-sync');
  assert.equal(
    identityLabel({ name: '__rsshub_cookie_sync_legacy__', legacy: true }),
    '旧版安装密钥（继续使用）',
  );
  assert.equal(identityLabel({ name: '../secret', legacy: false }), '');
});
