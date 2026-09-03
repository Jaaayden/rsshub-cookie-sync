import { COOKIE_PERMISSION_ORIGINS } from './lib/cookies.js';
import { createCookieCopyActions, createPopupActions } from './lib/popup-actions.js';

const PROVIDER_LABELS = Object.freeze({ zhihu: '知乎', weibo: '微博' });
const RESULT_LABELS = Object.freeze({
  unchanged: ['已同步', 'good'],
  candidate_saved: ['候选已保存', 'good'],
  promoted: ['已切换', 'good'],
  rejected_invalid: ['候选被拒绝', 'bad'],
  retryable_error: ['稍后重试', 'warning'],
  permission_required: ['需授权', 'warning'],
  missing_cookie: ['未找到 Cookie', 'warning'],
  paused: ['已暂停', 'warning'],
});
const REASON_LABELS = Object.freeze({
  permission_denied: '站点权限未授予',
  missing_cookie: '目标请求没有可用 Cookie',
  native_host_unavailable: 'Native Messaging Host 不可用',
  candidate_invalid: '服务器探针未通过',
  malformed_cookie: 'Cookie 格式异常',
  cookie_read_failed: '读取浏览器 Cookie 失败',
  permission_check_failed: '检查站点权限失败',
  hashing_failed: '生成状态指纹失败',
  invalid_response: '收到无效响应',
  upstream_temporary_failure: '上游暂时不可用',
  server_rejected: '服务器拒绝候选',
  ok: '',
});

const enabledElement = document.querySelector('#enabled');
const grantButton = document.querySelector('#grant');
const refreshButton = document.querySelector('#refresh');
const syncButton = document.querySelector('#sync');
const settingsButton = document.querySelector('#settings');
const noticeElement = document.querySelector('#notice');
const providersElement = document.querySelector('#providers');

function sendMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error('message_failed'));
        return;
      }
      resolve(response);
    });
  });
}

function showNotice(text, kind = '') {
  noticeElement.textContent = text;
  noticeElement.className = `notice${kind ? ` ${kind}` : ''}`;
}

function requestClipboardPermission() {
  const permissions = globalThis.chrome?.permissions;
  if (!permissions || typeof permissions.request !== 'function') {
    // The optional permission is supported by current Edge.  Falling through
    // here keeps the action usable in Chromium builds that expose clipboard
    // access without the permissions API; writeClipboard remains the final
    // authority and reports failure without exposing the Cookie.
    return Promise.resolve(true);
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      callback(value);
    };
    const callback = (granted) => {
      if (globalThis.chrome?.runtime?.lastError) {
        finish(reject, new Error('clipboard_permission_failed'));
        return;
      }
      finish(resolve, granted === true);
    };

    let returned;
    try {
      // Keep this call in the direct click path so Edge can associate the
      // optional permission prompt with the user's explicit action.
      returned = permissions.request({ permissions: ['clipboardWrite'] }, callback);
    } catch (error) {
      finish(reject, error);
      return;
    }
    if (returned && typeof returned.then === 'function') {
      returned.then(
        (granted) => finish(resolve, granted === true),
        (error) => finish(reject, error),
      );
    }
  });
}

async function writeClipboard(value) {
  if (!globalThis.navigator?.clipboard || typeof globalThis.navigator.clipboard.writeText !== 'function') {
    throw new Error('clipboard_unavailable');
  }
  await globalThis.navigator.clipboard.writeText(value);
}

const copyActions = createCookieCopyActions({
  sendMessage,
  confirmCopy: (provider) => {
    const label = PROVIDER_LABELS[provider] ?? provider;
    return globalThis.confirm(
      `Cookie 等同于 ${label} 登录凭证。复制后请只粘贴到可信位置，避免泄露。\n\n确定复制${label} Cookie？`,
    );
  },
  requestClipboardPermission,
  writeClipboard,
  showNotice,
  providerLabel: (provider) => PROVIDER_LABELS[provider] ?? provider,
});

function formatTime(value) {
  if (!Number.isFinite(value) || value <= 0) return '尚未同步';
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(new Date(value));
  } catch {
    return '已同步';
  }
}

function statusLabel(result, reason) {
  const [label] = RESULT_LABELS[result] ?? ['待同步', 'warning'];
  const reasonLabel = REASON_LABELS[reason];
  return reasonLabel ? `${label} · ${reasonLabel}` : label;
}

function renderProvider(provider, value, granted) {
  const card = document.createElement('article');
  card.className = 'provider';

  const name = document.createElement('div');
  name.className = 'provider-name';
  name.textContent = PROVIDER_LABELS[provider] ?? provider;

  const result = value?.lastResult ?? (granted ? null : 'permission_required');
  const reason = value?.lastReason;
  const [label, tone] = RESULT_LABELS[result] ?? ['待同步', 'warning'];
  const badge = document.createElement('span');
  badge.className = `badge ${tone}`;
  badge.textContent = statusLabel(result, reason);

  const meta = document.createElement('div');
  meta.className = 'provider-meta';
  const hash = typeof value?.hash === 'string' ? `指纹 ${value.hash.slice(0, 12)}…` : '尚无指纹';
  meta.textContent = `${granted ? '权限已授予' : '权限未授予'} · ${hash} · ${formatTime(value?.lastSyncAt)}`;

  const copyButton = document.createElement('button');
  copyButton.type = 'button';
  copyButton.className = 'copy-cookie secondary';
  copyButton.textContent = '复制 Cookie';
  copyButton.setAttribute('aria-label', `复制${PROVIDER_LABELS[provider] ?? provider} Cookie`);
  copyButton.addEventListener('click', () => {
    void copyActions.copyProviderCookie(provider, copyButton);
  });

  card.append(name, badge, meta, copyButton);
  return card;
}

function renderStatus(status) {
  enabledElement.checked = status?.enabled !== false;
  providersElement.replaceChildren();
  for (const provider of ['zhihu', 'weibo']) {
    providersElement.append(
      renderProvider(provider, status?.providers?.[provider], status?.permissions?.[provider] === true),
    );
  }
  grantButton.disabled = Object.values(status?.permissions ?? {}).every(Boolean);
}

const { refresh, refreshFromButton } = createPopupActions({
  sendMessage,
  renderStatus,
  showNotice,
});

grantButton.addEventListener('click', async () => {
  grantButton.disabled = true;
  showNotice('等待站点权限确认…');
  try {
    const granted = await chrome.permissions.request({ origins: COOKIE_PERMISSION_ORIGINS });
    if (!granted) {
      showNotice('未授予站点权限。', 'error');
      return;
    }
    showNotice('权限已授予，正在同步…');
    const response = await sendMessage({ type: 'sync-now' });
    if (!response?.ok) throw new Error('sync_failed');
    showNotice('同步请求已发送。', 'success');
  } catch {
    showNotice('权限请求失败，请重试。', 'error');
  } finally {
    // refresh() derives the disabled state from both optional origins. If the
    // background is unavailable, unlock the button so the user can retry.
    const refreshed = await refresh();
    if (!refreshed) grantButton.disabled = false;
  }
});

syncButton.addEventListener('click', async () => {
  syncButton.disabled = true;
  showNotice('正在读取并同步 Cookie…');
  try {
    const response = await sendMessage({ type: 'sync-now' });
    if (!response?.ok) throw new Error('sync_failed');
    showNotice('同步请求已发送。', 'success');
    // Read the state again after the upload finishes so the popup reflects
    // the background's persisted result, including concurrent alarm changes.
    await refresh();
  } catch {
    showNotice('同步失败，请检查站点权限或 Native Host。', 'error');
  } finally {
    syncButton.disabled = false;
  }
});

refreshButton.addEventListener('click', () => {
  void refreshFromButton(refreshButton);
});

settingsButton.addEventListener('click', async () => {
  settingsButton.disabled = true;
  try {
    await chrome.runtime.openOptionsPage();
  } catch {
    showNotice('无法打开连接设置，请从扩展详情页进入。', 'error');
  } finally {
    settingsButton.disabled = false;
  }
});

enabledElement.addEventListener('change', async () => {
  enabledElement.disabled = true;
  try {
    const response = await sendMessage({ type: 'set-enabled', enabled: enabledElement.checked });
    if (!response?.ok) throw new Error('toggle_failed');
    renderStatus(response);
    showNotice(enabledElement.checked ? '自动同步已启用。' : '自动同步已暂停。', 'success');
  } catch {
    enabledElement.checked = !enabledElement.checked;
    showNotice('更新开关失败，请重试。', 'error');
  } finally {
    enabledElement.disabled = false;
  }
});

void refresh();
