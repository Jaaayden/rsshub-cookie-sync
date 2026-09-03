import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { createPopupActions } from '../lib/popup-actions.js';

test('popup 点击绑定把实际刷新按钮传给只读刷新动作', async () => {
  const source = await readFile(new URL('../popup.js', import.meta.url), 'utf8');
  assert.match(
    source,
    /refreshButton\.addEventListener\('click',[\s\S]*refreshFromButton\(refreshButton\)/,
  );
});

test('刷新状态按钮只请求 get-status，不发起 sync-now 或 Cookie/上传操作', async () => {
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
  assert.deepEqual(notices, [['正在刷新状态…'], ['状态已刷新。', 'success']]);
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
  assert.deepEqual(notices, [['正在刷新状态…'], ['无法读取后台状态，请稍后重试。', 'error']]);
  assert.equal(refreshButton.disabled, false);
});
