import { COOKIE_PERMISSION_ORIGINS, PROVIDERS } from './lib/cookies.js';
import { createCookieCopyActions, createPopupActions, syncSummary } from './lib/popup-actions.js';

import { diagnosticReasonLabel, sanitizeDiagnosticLog } from './lib/protocol.js';

const PROVIDER_LABELS = Object.freeze({ zhihu: '知乎', weibo: '微博', twitter: 'X/Twitter' });
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
    const credentialLabel = provider === 'twitter' ? 'Auth Token' : 'Cookie';
    return globalThis.confirm(
      `${credentialLabel} 等同于 ${label} 登录凭证。复制后请只粘贴到可信位置，避免泄露。\n\n确定复制${label} ${credentialLabel}？`,
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
  badge.textContent = label;

  const meta = document.createElement('div');
  meta.className = 'provider-meta';
  const hash = typeof value?.hash === 'string' ? `指纹 ${value.hash.slice(0, 12)}…` : '尚无指纹';
  meta.textContent = `${granted ? '权限已授予' : '权限未授予'} · ${hash} · 最近尝试 ${formatTime(value?.lastSyncAt)}`;

  const copyButton = document.createElement('button');
  copyButton.type = 'button';
  copyButton.className = 'copy-cookie secondary';
  copyButton.textContent = provider === 'twitter' ? '复制 Auth Token' : '复制 Cookie';
  copyButton.setAttribute('aria-label', `复制${PROVIDER_LABELS[provider] ?? provider} ${provider === 'twitter' ? 'Auth Token' : 'Cookie'}`);
  copyButton.addEventListener('click', () => {
    void copyActions.copyProviderCookie(provider, copyButton);
  });

  const retryButton = document.createElement('button');
  retryButton.type = 'button';
  retryButton.className = 'secondary';
  retryButton.textContent = '重试此站点';
  retryButton.setAttribute('aria-label', `重试${PROVIDER_LABELS[provider]}`);
  retryButton.addEventListener('click', () => { void runSync(provider); });
  retryButton.disabled = syncing;
  const actions = document.createElement('div');
  actions.className = 'provider-actions';
  actions.append(retryButton, copyButton);
  const detail = document.createElement('div');
  detail.className = 'provider-meta';
  detail.textContent = reason ? (REASON_LABELS[reason] ?? diagnosticReasonLabel(reason)) :
    (result === 'retryable_error' ? '未返回具体原因，请展开诊断日志。' : '');
  card.append(name, badge, meta, detail, actions);
  return card;
}

function renderStatus(status) {
  enabledElement.checked = status?.enabled !== false;
  providersElement.replaceChildren();
  for (const provider of PROVIDERS) {
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
    const summary = syncSummary(response.providers);
    showNotice(summary.text, summary.kind);
  } catch {
    showNotice('权限请求失败，请重试。', 'error');
  } finally {
    // refresh() derives the disabled state from both optional origins. If the
    // background is unavailable, unlock the button so the user can retry.
    const refreshed = await refresh();
    if (!refreshed) grantButton.disabled = false;
  }
});

let syncing = false;
async function runSync(provider) {
  if (syncing) return;
  syncing = true;
  syncButton.disabled = true;
  document.querySelectorAll('.provider-actions button').forEach((button) => { button.disabled = true; });
  showNotice(provider ? `正在重试${PROVIDER_LABELS[provider]}…` : '正在读取并同步 Cookie…');
  try {
    const response = await sendMessage(provider ? { type: 'sync-provider', provider } : { type: 'sync-now' });
    if (!response?.ok) throw new Error('sync_failed');
    renderStatus(response);
    const summary = syncSummary(response.providers, provider ? [provider] : PROVIDERS);
    showNotice(summary.text, summary.kind);
  } catch {
    showNotice('无法完成同步请求，请查看诊断日志和 Native Host 状态。', 'error');
  } finally {
    syncing = false;
    syncButton.disabled = false;
    document.querySelectorAll('.provider-actions button').forEach((button) => { button.disabled = false; });
    if (document.querySelector('#diagnostics').open) await loadDiagnostics();
  }
}

syncButton.addEventListener('click', () => { void runSync(); });

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

const logOutput = document.querySelector('#diagnostic-output');
const logNotice = document.querySelector('#diagnostic-notice');
const stageLabels = { collect: '采集', native: '上传及服务端处理', complete: '完成' };
async function loadDiagnostics() {
  try {
    const response = await sendMessage({ type: 'get-diagnostics' });
    if (!response?.ok) throw new Error('unavailable');
    const entries = sanitizeDiagnosticLog(response.entries);
    logOutput.textContent = entries.length ? entries.slice().reverse().map((entry) =>
      `${new Date(entry.time).toLocaleString()} · ${PROVIDER_LABELS[entry.provider]} · ${entry.trigger === 'manual' ? '手动' : '自动'}\n${stageLabels[entry.stage]} / ${RESULT_LABELS[entry.status]?.[0] ?? (entry.status === 'started' ? '开始' : entry.status)}${entry.durationMs === null ? '' : ` / ${entry.durationMs}ms`}${entry.stage === 'complete' && entry.reason ? `\n${REASON_LABELS[entry.reason] ?? diagnosticReasonLabel(entry.reason)}` : ''}${entry.stage === 'complete' && entry.status === 'retryable_error' && !entry.reason ? '\n未返回具体原因，请确认 Host 和服务端均已升级。' : ''}`
    ).join('\n\n') : '暂无日志。点击某个站点的“重试此站点”后再刷新。';
    logNotice.textContent = `最近 ${entries.length} 条记录（最多 200 条）。`;
    return entries;
  } catch {
    logNotice.textContent = '无法读取诊断日志，请重新加载扩展后重试。';
    return null;
  }
}
document.querySelector('#diagnostics').addEventListener('toggle', (event) => {
  if (event.target.open) void loadDiagnostics();
});
document.querySelector('#diagnostic-refresh').addEventListener('click', () => { void loadDiagnostics(); });
document.querySelector('#diagnostic-clear').addEventListener('click', async () => {
  try {
    const response = await sendMessage({ type: 'clear-diagnostics' });
    if (!response?.ok) throw new Error('unavailable');
    await loadDiagnostics();
  } catch { logNotice.textContent = '清空日志失败，请重试。'; }
});
document.querySelector('#diagnostic-export').addEventListener('click', async () => {
  const entries = await loadDiagnostics();
  if (entries === null) return;
  const blob = new Blob([JSON.stringify({ version: 1, extensionVersion: chrome.runtime.getManifest().version, entries }, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'rsshub-cookie-sync-diagnostics.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
