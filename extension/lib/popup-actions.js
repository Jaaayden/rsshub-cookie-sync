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
    showNotice('正在刷新状态…');
    try {
      const refreshed = await refresh();
      if (!refreshed) return false;
      showNotice('状态已刷新。', 'success');
      return true;
    } finally {
      button.disabled = false;
    }
  }

  return Object.freeze({ refresh, refreshFromButton });
}
