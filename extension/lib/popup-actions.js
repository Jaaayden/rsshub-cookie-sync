import { PROVIDERS, validateCookieHeader } from './cookies.js';

/**
 * Actions shared by the popup UI.
 *
 * Keeping the status refresh path independent from the DOM makes its read-only
 * contract explicit and testable: refreshing asks the background worker for
 * its persisted status and does not collect or transmit Cookie data itself.
 */
export function createPopupActions({ sendMessage, renderStatus, showNotice }) {
  if (typeof sendMessage !== 'function') throw new TypeError('sendMessage must be a function');
  if (typeof renderStatus !== 'function') throw new TypeError('renderStatus must be a function');
  if (typeof showNotice !== 'function') throw new TypeError('showNotice must be a function');

  async function refresh() {
    try {
      const response = await sendMessage({ type: 'get-status' });
      if (!response?.ok) throw new Error('status_failed');
      renderStatus(response);
      return true;
    } catch {
      showNotice('无法读取后台状态，请稍后重试。', 'error');
      return false;
    }
  }

  async function refreshFromButton(button) {
    if (!button || typeof button !== 'object') {
      throw new TypeError('button must be an object');
    }
    button.disabled = true;
    showNotice('正在刷新扩展状态…');
    try {
      const refreshed = await refresh();
      if (!refreshed) return false;
      showNotice('扩展状态已刷新。', 'success');
      return true;
    } finally {
      button.disabled = false;
    }
  }

  return Object.freeze({ refresh, refreshFromButton });
}

/**
 * Explicit, one-provider Cookie copy action for the popup.
 *
 * The caller must obtain confirmation before asking the background worker for
 * the Cookie.  The returned Cookie is kept only in the current call stack and
 * handed directly to the injected clipboard writer; this module never writes
 * it to extension storage or any other durable location.
 */
export function createCookieCopyActions({
  sendMessage,
  confirmCopy,
  requestClipboardPermission,
  writeClipboard,
  showNotice,
  providerLabel = (provider) => provider,
}) {
  if (typeof sendMessage !== 'function') throw new TypeError('sendMessage must be a function');
  if (typeof confirmCopy !== 'function') throw new TypeError('confirmCopy must be a function');
  if (typeof requestClipboardPermission !== 'function') {
    throw new TypeError('requestClipboardPermission must be a function');
  }
  if (typeof writeClipboard !== 'function') throw new TypeError('writeClipboard must be a function');
  if (typeof showNotice !== 'function') throw new TypeError('showNotice must be a function');
  if (typeof providerLabel !== 'function') throw new TypeError('providerLabel must be a function');

  async function copyProviderCookie(provider, button) {
    if (!PROVIDERS.includes(provider)) {
      showNotice('不支持的服务。', 'error');
      return false;
    }
    if (!button || typeof button !== 'object') {
      throw new TypeError('button must be an object');
    }

    const label = providerLabel(provider);
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = '复制中…';

    try {
      // This is deliberately synchronous: the browser's permission prompt
      // should remain associated with the explicit button click.
      let confirmed;
      try {
        confirmed = confirmCopy(provider);
      } catch {
        showNotice(`复制${label} Cookie 前的确认失败。`, 'error');
        return false;
      }
      if (confirmed !== true) {
        showNotice(`已取消复制${label} Cookie。`);
        return false;
      }

      let permissionGranted;
      try {
        // Ask for the optional clipboard permission before reading the Cookie,
        // so a denied prompt never causes a Cookie read.
        permissionGranted = await requestClipboardPermission();
      } catch {
        permissionGranted = false;
      }
      if (permissionGranted !== true) {
        showNotice(`未获得剪贴板权限，已取消复制${label} Cookie。`, 'error');
        return false;
      }

      let response;
      try {
        response = await sendMessage({ type: 'copy-cookie', provider });
      } catch {
        showNotice(`读取${label} Cookie 失败，请重试。`, 'error');
        return false;
      }
      if (
        !response?.ok ||
        response.provider !== provider ||
        typeof response.cookieHeader !== 'string'
      ) {
        const reason = response?.error;
        if (reason === 'permission_required') {
          showNotice(`请先授权${label}站点权限。`, 'error');
        } else if (reason === 'missing_cookie') {
          showNotice(`未找到${label} Cookie，请先登录。`, 'error');
        } else {
          showNotice(`读取${label} Cookie 失败，请重试。`, 'error');
        }
        return false;
      }

      try {
        // Validate before handing data to the clipboard writer.  The value is
        // never logged, rendered, or persisted by this action.
        validateCookieHeader(response.cookieHeader);
        await writeClipboard(response.cookieHeader);
      } catch {
        showNotice(`复制${label} Cookie 失败，请检查剪贴板权限。`, 'error');
        return false;
      }

      showNotice(
        `${label} Cookie 已复制。它等同于登录凭证，请勿粘贴到聊天或公开位置。`,
        'success',
      );
      return true;
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  return Object.freeze({ copyProviderCookie });
}
