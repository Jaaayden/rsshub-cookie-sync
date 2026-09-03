/**
 * Pure helpers for the Native Messaging connection settings page.
 *
 * The extension deliberately keeps this configuration out of
 * chrome.storage.  These helpers only validate the small, non-secret control
 * payload sent to the Native Host and reduce its response to a fixed shape.
 */

export const NATIVE_CONFIG_DEFAULTS = Object.freeze({
  host: '',
  port: 22,
  user: 'rsshub-sync',
  identityName: 'rsshub-cookie-sync',
});

export const NATIVE_CONFIG_ACTIONS = Object.freeze({
  get: 'get-config',
  set: 'set-config',
});

const HOST_RE = /^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$/u;
const USER_RE = /^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/u;
const IDENTITY_RE = /^[A-Za-z0-9._+-]{1,128}$/u;
const SAFE_ERROR_CODES = new Set([
  'configuration_missing',
  'configuration_invalid',
  'configuration_error',
  'identity_missing',
  'known_hosts_missing',
  'invalid_request',
  'unsupported_request',
  'native_host_unavailable',
  'invalid_response',
]);

function validPort(value) {
  return (
    typeof value === 'number' &&
    Number.isInteger(value) &&
    value >= 1 &&
    value <= 65535
  );
}

function normalizeHost(value, { allowEmpty = false } = {}) {
  if (typeof value !== 'string') return null;
  const host = value.trim();
  if (allowEmpty && host === '') return '';
  if (!HOST_RE.test(host) || host.startsWith('.') || host.endsWith('.')) return null;
  return host;
}

function normalizeUser(value) {
  if (typeof value !== 'string') return null;
  const user = value.trim();
  return USER_RE.test(user) ? user : null;
}

function normalizeIdentityName(value) {
  if (typeof value !== 'string') return null;
  const name = value.trim();
  return IDENTITY_RE.test(name) ? name : null;
}

function configFromObject(value, { allowEmptyHost = true } = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;

  // The control protocol uses a small nested server object plus one identity
  // selector. Accept the equivalent flat form for pure-helper callers, while
  // always returning the same flat, redacted shape to the UI.
  const server = value.server && typeof value.server === 'object' ? value.server : value;
  const ssh = value.ssh && typeof value.ssh === 'object' ? value.ssh : value;
  const host =
    server.host === null && allowEmptyHost
      ? ''
      : normalizeHost(server.host, { allowEmpty: allowEmptyHost });
  const port = server.port;
  const user = normalizeUser(server.user ?? value.user ?? NATIVE_CONFIG_DEFAULTS.user);
  const identityName = normalizeIdentityName(
    value.identityName ?? ssh.identityName ?? ssh.identity_name,
  );
  if (host === null || !validPort(port) || user === null || identityName === null) {
    return null;
  }
  return Object.freeze({ host, port, user, identityName });
}

/**
 * Validate values read from HTML form controls and return a native-safe
 * configuration.  The host is required when saving, but may be empty while a
 * newly installed Native Host is being inspected for the first time.
 */
export function normalizeNativeConfigInput(raw, { allowEmptyHost = false } = {}) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const value = {
    host: raw.host,
    port:
      typeof raw.port === 'string' && raw.port.trim() !== ''
        ? Number(raw.port)
        : raw.port,
    user: raw.user,
    identityName: raw.identityName,
  };
  return configFromObject(value, { allowEmptyHost });
}

/** Return the exact, non-secret Native Messaging control request. */
export function createGetNativeConfigMessage() {
  return Object.freeze({ version: 1, action: NATIVE_CONFIG_ACTIONS.get });
}

export function createSetNativeConfigMessage(raw) {
  const config = normalizeNativeConfigInput(raw, { allowEmptyHost: false });
  if (!config) throw new TypeError('invalid native configuration');
  return Object.freeze({
    version: 1,
    action: NATIVE_CONFIG_ACTIONS.set,
    server: Object.freeze({
      host: config.host,
      port: config.port,
      user: config.user,
    }),
    identityName: config.identityName,
  });
}

function sanitizeIdentity(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const name = normalizeIdentityName(value.name);
  if (!name || typeof value.legacy !== 'boolean') return null;
  return Object.freeze({ name, legacy: value.legacy });
}

function sanitizeError(value) {
  return typeof value === 'string' && SAFE_ERROR_CODES.has(value)
    ? value
    : 'invalid_response';
}

/**
 * Reduce a Native Host response to fields the settings page is allowed to
 * see.  No arbitrary host text, private-key path, Cookie, or command output
 * can be propagated to the DOM.
 */
export function sanitizeNativeConfigResponse(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { ok: false, error: 'invalid_response' };
  }

  if (raw.ok !== true && !['ok', 'config', 'config_saved'].includes(raw.status)) {
    if (raw.status === 'config_error') {
      return { ok: false, error: 'configuration_error' };
    }
    if (raw.status === 'rejected_invalid') {
      return { ok: false, error: 'invalid_request' };
    }
    return { ok: false, error: sanitizeError(raw.error) };
  }

  const config = configFromObject(
    raw.config ?? { server: raw.server, identityName: raw.identityName },
    { allowEmptyHost: true },
  );
  if (!config) return { ok: false, error: 'invalid_response' };

  const identities = [];
  if (Array.isArray(raw.identities)) {
    for (const candidate of raw.identities) {
      const identity = sanitizeIdentity(candidate);
      if (identity && !identities.some((item) => item.name === identity.name)) {
        identities.push(identity);
      }
    }
  }
  if (!identities.some((item) => item.name === config.identityName)) {
    identities.unshift({ name: config.identityName, legacy: false });
  }

  return {
    ok: true,
    config,
    identities: Object.freeze(identities),
  };
}

export function nativeConfigErrorMessage(error) {
  const labels = Object.freeze({
    configuration_missing: '尚未配置连接信息。',
    configuration_invalid: '连接配置无效，请检查输入。',
    configuration_error: 'Native Host 尚未准备好连接配置。',
    identity_missing: '找不到所选 SSH 私钥，请先在 ~/.ssh/ 准备好它。',
    known_hosts_missing: '尚未配置目标服务器的 SSH 主机指纹。',
    invalid_request: 'Native Host 拒绝了设置请求。',
    unsupported_request: 'Native Host 版本过旧，请重新安装。',
    native_host_unavailable: 'Native Messaging Host 不可用，请检查本机安装。',
    invalid_response: 'Native Host 返回了无效状态。',
  });
  return labels[error] ?? labels.invalid_response;
}

export function identityLabel(identity) {
  if (!identity || typeof identity !== 'object') return '';
  const name = normalizeIdentityName(identity.name);
  if (!name) return '';
  return identity.legacy === true ? '旧版安装密钥（继续使用）' : name;
}
