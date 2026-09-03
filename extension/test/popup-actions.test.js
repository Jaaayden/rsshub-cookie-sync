import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { createCookieCopyActions, createPopupActions } from '../lib/popup-actions.js';

test('popup 点击绑定把实际刷新按钮传给只读刷新动作', async () => {
  const source = await readFile(new URL('../popup.js', import.meta.url), 'utf8');
  assert.match(
    source,
    /refreshButton\.addEventListener\('click',[\s\S]*refreshFromButton\(refreshButton\)/,
  );
});

test('刷新扩展状态按钮只请求 get-status，不发起 sync-now 或 Cookie/上传操作', async () => {
  const messages = [];
  const cookieReads = [];
  const uploads = [];
  const rendered = [];
  const notices = [];
  const sendMessage = async (message) => {
    messages.push(message);
    if (message.type === 'sync-now') {
      uploads.push(message);
      throw new Error('refresh must not request sync-now');
    }
    if (message.type !== 'get-status') {
      throw new Error(`unexpected popup message: ${message.type}`);
    }
    return {
      ok: true,
      enabled: true,
      permissions: { zhihu: true, weibo: true },
      providers: {
        zhihu: { hash: null, lastSyncAt: null, lastResult: null, lastReason: null },
        weibo: { hash: null, lastSyncAt: null, lastResult: null, lastReason: null },
      },
    };
  };
  const actions = createPopupActions({
    sendMessage,
    renderStatus: (status) => rendered.push(status),
    showNotice: (...args) => notices.push(args),
  });
  const refreshButton = { disabled: false };

  assert.equal(await actions.refreshFromButton(refreshButton), true);
  assert.deepEqual(messages, [{ type: 'get-status' }]);
  assert.deepEqual(rendered, [
    {
      ok: true,
      enabled: true,
      permissions: { zhihu: true, weibo: true },
      providers: {
        zhihu: { hash: null, lastSyncAt: null, lastResult: null, lastReason: null },
        weibo: { hash: null, lastSyncAt: null, lastResult: null, lastReason: null },
      },
    },
  ]);
  assert.deepEqual(notices, [['正在刷新扩展状态…'], ['扩展状态已刷新。', 'success']]);
  assert.deepEqual(cookieReads, []);
  assert.deepEqual(uploads, []);
  assert.equal(refreshButton.disabled, false);
});

test('刷新请求失败时只显示错误并恢复按钮状态', async () => {
  const messages = [];
  const notices = [];
  const actions = createPopupActions({
    sendMessage: async (message) => {
      messages.push(message);
      return { ok: false, error: 'internal_error' };
    },
    renderStatus: () => {
      throw new Error('failed refresh must not render a partial status');
    },
    showNotice: (...args) => notices.push(args),
  });
  const refreshButton = { disabled: false };

  assert.equal(await actions.refreshFromButton(refreshButton), false);
  assert.deepEqual(messages, [{ type: 'get-status' }]);
  assert.deepEqual(notices, [['正在刷新扩展状态…'], ['无法读取后台状态，请稍后重试。', 'error']]);
  assert.equal(refreshButton.disabled, false);
});

test('每个服务卡片绑定自己的复制按钮，并在显式确认后只读取该服务', async () => {
  const source = await readFile(new URL('../popup.js', import.meta.url), 'utf8');
  assert.match(
    source,
    /copyButton\.addEventListener\('click',[\s\S]*copyActions\.copyProviderCookie\(provider, copyButton\)/,
  );

  const messages = [];
  const confirmations = [];
  const permissionRequests = [];
  const clipboardWrites = [];
  const notices = [];
  const storageWrites = [];
  const actions = createCookieCopyActions({
    sendMessage: async (message) => {
      messages.push(message);
      return { ok: true, provider: message.provider, cookieHeader: 'z_c0=token=a=b' };
    },
    confirmCopy: (provider) => {
      confirmations.push(provider);
      return true;
    },
    requestClipboardPermission: async () => {
      permissionRequests.push(true);
      return true;
    },
    writeClipboard: async (value) => {
      clipboardWrites.push(value);
    },
    showNotice: (...args) => notices.push(args),
    providerLabel: (provider) => ({ zhihu: '知乎', weibo: '微博' })[provider],
  });
  const button = { disabled: false, textContent: '复制 Cookie' };

  assert.equal(await actions.copyProviderCookie('zhihu', button), true);
  assert.deepEqual(messages, [{ type: 'copy-cookie', provider: 'zhihu' }]);
  assert.deepEqual(confirmations, ['zhihu']);
  assert.deepEqual(permissionRequests, [true]);
  assert.deepEqual(clipboardWrites, ['z_c0=token=a=b']);
  assert.match(notices.at(-1)?.[0], /^知乎 Cookie 已复制/);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '复制 Cookie');
  assert.deepEqual(storageWrites, [], '复制动作不得写入任何持久化存储');
});

test('取消风险确认时不请求权限、不读取 Cookie，也不改动剪贴板', async () => {
  const messages = [];
  const permissionRequests = [];
  const clipboardWrites = [];
  const notices = [];
  const actions = createCookieCopyActions({
    sendMessage: async (message) => {
      messages.push(message);
      throw new Error('Cookie must not be read after confirmation is declined');
    },
    confirmCopy: () => false,
    requestClipboardPermission: async () => {
      permissionRequests.push(true);
      return true;
    },
    writeClipboard: async (value) => {
      clipboardWrites.push(value);
    },
    showNotice: (...args) => notices.push(args),
  });
  const button = { disabled: false, textContent: '复制 Cookie' };

  assert.equal(await actions.copyProviderCookie('weibo', button), false);
  assert.deepEqual(messages, []);
  assert.deepEqual(permissionRequests, []);
  assert.deepEqual(clipboardWrites, []);
  assert.deepEqual(notices, [['已取消复制weibo Cookie。']]);
  assert.equal(button.disabled, false);
});

test('拒绝剪贴板权限时不会读取 Cookie', async () => {
  const messages = [];
  const clipboardWrites = [];
  const notices = [];
  const actions = createCookieCopyActions({
    sendMessage: async (message) => {
      messages.push(message);
      throw new Error('Cookie must not be read when clipboard permission is denied');
    },
    confirmCopy: () => true,
    requestClipboardPermission: async () => false,
    writeClipboard: async (value) => {
      clipboardWrites.push(value);
    },
    showNotice: (...args) => notices.push(args),
    providerLabel: () => '微博',
  });
  const button = { disabled: false, textContent: '复制 Cookie' };

  assert.equal(await actions.copyProviderCookie('weibo', button), false);
  assert.deepEqual(messages, []);
  assert.deepEqual(clipboardWrites, []);
  assert.deepEqual(notices, [['未获得剪贴板权限，已取消复制微博 Cookie。', 'error']]);
  assert.equal(button.disabled, false);
});

test('服务端读取失败和剪贴板写入失败都会恢复按钮并显示明确状态', async () => {
  const failureNotices = [];
  const readFailure = createCookieCopyActions({
    sendMessage: async () => ({ ok: false, error: 'missing_cookie' }),
    confirmCopy: () => true,
    requestClipboardPermission: async () => true,
    writeClipboard: async () => {
      throw new Error('must not write after server failure');
    },
    showNotice: (...args) => failureNotices.push(args),
    providerLabel: () => '知乎',
  });
  const readFailureButton = { disabled: false, textContent: '复制 Cookie' };
  assert.equal(await readFailure.copyProviderCookie('zhihu', readFailureButton), false);
  assert.deepEqual(failureNotices, [['未找到知乎 Cookie，请先登录。', 'error']]);
  assert.equal(readFailureButton.disabled, false);

  const clipboardFailureNotices = [];
  const clipboardFailure = createCookieCopyActions({
    sendMessage: async () => ({ ok: true, provider: 'zhihu', cookieHeader: 'z_c0=secret' }),
    confirmCopy: () => true,
    requestClipboardPermission: async () => true,
    writeClipboard: async () => {
      throw new Error('clipboard is unavailable');
    },
    showNotice: (...args) => clipboardFailureNotices.push(args),
    providerLabel: () => '知乎',
  });
  const clipboardFailureButton = { disabled: false, textContent: '复制 Cookie' };
  assert.equal(await clipboardFailure.copyProviderCookie('zhihu', clipboardFailureButton), false);
  assert.deepEqual(clipboardFailureNotices, [['复制知乎 Cookie 失败，请检查剪贴板权限。', 'error']]);
  assert.equal(clipboardFailureButton.disabled, false);
});

test('未知服务不会触发读取或复制', async () => {
  const messages = [];
  const notices = [];
  const actions = createCookieCopyActions({
    sendMessage: async (message) => {
      messages.push(message);
    },
    confirmCopy: () => true,
    requestClipboardPermission: async () => true,
    writeClipboard: async () => undefined,
    showNotice: (...args) => notices.push(args),
  });

  assert.equal(await actions.copyProviderCookie('github', { disabled: false, textContent: '复制 Cookie' }), false);
  assert.deepEqual(messages, []);
  assert.deepEqual(notices, [['不支持的服务。', 'error']]);
});
