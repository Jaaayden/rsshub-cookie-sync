import {
  NATIVE_CONFIG_DEFAULTS,
  identityLabel,
  nativeConfigErrorMessage,
  normalizeNativeConfigInput,
  sanitizeNativeConfigResponse,
} from './lib/native-config.js';

const form = document.querySelector('#settings-form');
const hostElement = document.querySelector('#server-host');
const portElement = document.querySelector('#server-port');
const identityElement = document.querySelector('#identity-name');
const refreshButton = document.querySelector('#refresh');
const saveButton = document.querySelector('#save');
const cancelButton = document.querySelector('#cancel');
const noticeElement = document.querySelector('#notice');

function sendMessage(message) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendMessage(message, (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error('message_failed'));
          return;
        }
        resolve(response);
      });
    } catch {
      reject(new Error('message_failed'));
    }
  });
}

function showNotice(text, kind = '') {
  noticeElement.textContent = text;
  noticeElement.className = `notice${kind ? ` ${kind}` : ''}`;
}

function setBusy(busy) {
  refreshButton.disabled = busy;
  saveButton.disabled = busy;
  cancelButton.disabled = busy;
}

function setFormConfig(config) {
  hostElement.value = config.host;
  portElement.value = String(config.port);
  identityElement.value = config.identityName;
}

function renderIdentities(identities, selectedName) {
  identityElement.replaceChildren();
  const items = Array.isArray(identities) ? identities : [];
  const seen = new Set();
  for (const identity of items) {
    const label = identityLabel(identity);
    const name = identity?.name;
    if (!label || typeof name !== 'string' || seen.has(name)) continue;
    seen.add(name);
    const option = document.createElement('option');
    option.value = name;
    option.textContent = label;
    identityElement.append(option);
  }

  if (!seen.has(selectedName)) {
    const option = document.createElement('option');
    option.value = selectedName;
    option.textContent = selectedName;
    identityElement.prepend(option);
  }
  identityElement.value = selectedName;
}

function formConfig() {
  return normalizeNativeConfigInput({
    host: hostElement.value,
    port: portElement.value,
    user: NATIVE_CONFIG_DEFAULTS.user,
    identityName: identityElement.value,
  });
}

async function loadConfig({ announce = true } = {}) {
  if (announce) showNotice('正在读取本机连接设置…');
  setBusy(true);
  try {
    // This control request contains no Cookie and the background's handler
    // never accesses the browser cookie API for it.
    const raw = await sendMessage({ type: 'get-native-config' });
    const response = sanitizeNativeConfigResponse(raw);
    if (!response.ok) {
      showNotice(nativeConfigErrorMessage(response.error), 'error');
      return false;
    }
    renderIdentities(response.identities, response.config.identityName);
    setFormConfig(response.config);
    if (announce) showNotice('连接设置已刷新。', 'success');
    return true;
  } catch {
    showNotice(nativeConfigErrorMessage('native_host_unavailable'), 'error');
    return false;
  } finally {
    setBusy(false);
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const config = formConfig();
  if (!config) {
    showNotice('请填写有效的服务器地址、端口并选择 SSH 密钥文件名。', 'error');
    return;
  }

  setBusy(true);
  showNotice('正在保存连接设置…');
  try {
    const raw = await sendMessage({
      type: 'set-native-config',
      config,
    });
    const response = sanitizeNativeConfigResponse(raw);
    if (!response.ok) {
      showNotice(nativeConfigErrorMessage(response.error), 'error');
      return;
    }
    renderIdentities(response.identities, response.config.identityName);
    setFormConfig(response.config);
    showNotice('连接设置已保存。', 'success');
  } catch {
    showNotice(nativeConfigErrorMessage('native_host_unavailable'), 'error');
  } finally {
    setBusy(false);
  }
});

refreshButton.addEventListener('click', () => {
  void loadConfig();
});

cancelButton.addEventListener('click', () => {
  window.close();
  // Browsers may ignore window.close() for a manually opened options tab.
  // Going back is still local-only and does not touch Cookie state.
  if (window.history.length > 1) window.history.back();
});

// Render safe defaults while the Native Host request is in flight.  The
// default host is intentionally empty: a deployment-specific address must be
// entered by the operator and is never embedded in this extension.
renderIdentities([{ name: NATIVE_CONFIG_DEFAULTS.identityName }], NATIVE_CONFIG_DEFAULTS.identityName);
setFormConfig(NATIVE_CONFIG_DEFAULTS);
void loadConfig({ announce: false });
