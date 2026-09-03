/**
 * Cookie collection and serialization helpers shared by the extension and
 * the Node test suite.  This module intentionally has no browser imports.
 */

export const COOKIE_TARGETS = Object.freeze({
  zhihu: Object.freeze({
    url: 'https://www.zhihu.com/api/v3/moments',
    host: 'www.zhihu.com',
    path: '/api/v3/moments',
    permissionOrigins: Object.freeze([
      'https://zhihu.com/*',
      'https://www.zhihu.com/*',
    ]),
  }),
  weibo: Object.freeze({
    url: 'https://m.weibo.cn/feed/group',
    host: 'm.weibo.cn',
    path: '/feed/group',
    permissionOrigins: Object.freeze([
      'https://weibo.cn/*',
      'https://m.weibo.cn/*',
    ]),
  }),
});

export const PROVIDERS = Object.freeze(Object.keys(COOKIE_TARGETS));
export const COOKIE_PERMISSION_ORIGINS = Object.freeze(
  PROVIDERS.flatMap((provider) => COOKIE_TARGETS[provider].permissionOrigins),
);

export const MAX_COOKIE_HEADER_LENGTH = 64 * 1024;

const COOKIE_NAME_RE = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const CONTROL_RE = /[\u0000\r\n]/u;

export function getCookieTarget(provider) {
  const target = COOKIE_TARGETS[provider];
  if (!target) {
    throw new Error(`unknown provider: ${provider}`);
  }
  return target;
}

/**
 * Return true only for the HTTPS endpoint used to collect this provider's
 * cookies. Query strings are allowed because they do not affect cookie
 * matching, but a different host, port, scheme, or path is not.
 */
export function isCookieTargetUrl(provider, value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }

  const target = getCookieTarget(provider);
  return (
    parsed.protocol === 'https:' &&
    parsed.hostname.toLowerCase() === target.host &&
    parsed.port === '' &&
    parsed.pathname === target.path
  );
}

function domainMatches(host, domain, hostOnly) {
  if (typeof domain !== 'string' || domain.length === 0) {
    return false;
  }
  const normalizedDomain = domain.replace(/^\.+/u, '').toLowerCase();
  if (!normalizedDomain) {
    return false;
  }
  if (hostOnly === true) {
    return host === normalizedDomain;
  }
  return host === normalizedDomain || host.endsWith(`.${normalizedDomain}`);
}

function pathMatches(requestPath, cookiePath) {
  const normalizedCookiePath =
    typeof cookiePath === 'string' && cookiePath.startsWith('/')
      ? cookiePath
      : '/';

  if (requestPath === normalizedCookiePath) {
    return true;
  }
  if (!requestPath.startsWith(normalizedCookiePath)) {
    return false;
  }
  if (normalizedCookiePath.endsWith('/')) {
    return true;
  }
  return requestPath[normalizedCookiePath.length] === '/';
}

/**
 * Match a Cookie API record against the exact endpoint that will receive the
 * serialized header. This mirrors the domain/path/secure parts of RFC 6265.
 */
export function cookieAppliesToTarget(provider, cookie) {
  if (!cookie || typeof cookie !== 'object') {
    return false;
  }
  const target = getCookieTarget(provider);
  const domain = typeof cookie.domain === 'string' ? cookie.domain : '';
  const host = target.host;

  return (
    cookie.secure !== true || target.url.startsWith('https://')
  ) && domainMatches(host, domain, cookie.hostOnly) && pathMatches(target.path, cookie.path);
}

export function applicableCookies(provider, cookies) {
  if (!Array.isArray(cookies)) {
    return [];
  }
  return cookies.filter((cookie) => cookieAppliesToTarget(provider, cookie));
}

function compareText(a, b) {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

function cookieSortKey(cookie) {
  return {
    name: cookie.name,
    value: cookie.value,
    domain: typeof cookie.domain === 'string' ? cookie.domain.toLowerCase() : '',
    path: typeof cookie.path === 'string' && cookie.path.startsWith('/') ? cookie.path : '/',
    secure: cookie.secure === true ? '1' : '0',
    hostOnly: cookie.hostOnly === true ? '1' : '0',
    storeId: typeof cookie.storeId === 'string' ? cookie.storeId : '',
  };
}

function compareCookieRecords(left, right) {
  const a = cookieSortKey(left);
  const b = cookieSortKey(right);

  // Browsers send the longest matching path first. The remaining fields make
  // the result deterministic even when cookies.onChanged/getAll reorder rows.
  const pathLength = b.path.length - a.path.length;
  if (pathLength !== 0) return pathLength;
  return (
    compareText(a.name, b.name) ||
    compareText(a.domain, b.domain) ||
    compareText(a.path, b.path) ||
    compareText(a.value, b.value) ||
    compareText(a.secure, b.secure) ||
    compareText(a.hostOnly, b.hostOnly) ||
    compareText(a.storeId, b.storeId)
  );
}

function assertSafeCookiePart(part, label) {
  if (typeof part !== 'string' || part.length === 0) {
    throw new TypeError(`${label} must be a non-empty string`);
  }
  if (CONTROL_RE.test(part)) {
    throw new TypeError(`${label} contains a control character`);
  }
}

/**
 * Serialize every applicable record into a Cookie request header. Exact
 * duplicate records are removed, while different cookies with the same name
 * are retained: both may be valid when they come from different paths or
 * domains, and silently dropping one would make collection incomplete.
 */
export function serializeCookieHeader(cookies) {
  if (!Array.isArray(cookies)) {
    throw new TypeError('cookies must be an array');
  }

  const unique = new Map();
  for (const cookie of cookies) {
    if (!cookie || typeof cookie !== 'object') continue;
    assertSafeCookiePart(cookie.name, 'cookie name');
    if (cookie.name.includes(';') || !COOKIE_NAME_RE.test(cookie.name)) {
      throw new TypeError('cookie name contains an invalid character');
    }
    if (typeof cookie.value !== 'string') {
      throw new TypeError('cookie value must be a string');
    }
    if (CONTROL_RE.test(cookie.value) || cookie.value.includes(';')) {
      throw new TypeError('cookie value contains an invalid character');
    }
    const key = [
      cookie.name,
      cookie.value,
      typeof cookie.domain === 'string' ? cookie.domain.toLowerCase() : '',
      typeof cookie.path === 'string' ? cookie.path : '/',
      cookie.secure === true ? '1' : '0',
      cookie.hostOnly === true ? '1' : '0',
      typeof cookie.storeId === 'string' ? cookie.storeId : '',
    ].join('\u001f');
    unique.set(key, cookie);
  }

  const normalized = [...unique.values()].sort(compareCookieRecords);
  const header = normalized.map((cookie) => `${cookie.name}=${cookie.value}`).join('; ');
  validateCookieHeader(header, { allowEmpty: true });
  return header;
}

export function validateCookieHeader(value, { allowEmpty = false } = {}) {
  if (typeof value !== 'string') {
    throw new TypeError('cookieHeader must be a string');
  }
  if (!allowEmpty && value.length === 0) {
    throw new TypeError('cookieHeader must not be empty');
  }
  const byteLength = new TextEncoder().encode(value).length;
  if (byteLength > MAX_COOKIE_HEADER_LENGTH) {
    throw new RangeError(`cookieHeader exceeds ${MAX_COOKIE_HEADER_LENGTH} bytes`);
  }
  if (CONTROL_RE.test(value)) {
    throw new TypeError('cookieHeader contains a control character');
  }

  if (value.length > 0) {
    for (const pair of value.split(';')) {
      const equals = pair.indexOf('=');
      if (equals < 0) {
        throw new TypeError('cookieHeader contains a pair without equals');
      }
      const name = pair.slice(0, equals).trim();
      if (!COOKIE_NAME_RE.test(name)) {
        throw new TypeError('cookieHeader contains an invalid cookie name');
      }
    }
  }
  return value;
}

export async function sha256Hex(value, subtle = globalThis.crypto?.subtle) {
  if (typeof value !== 'string') {
    throw new TypeError('value must be a string');
  }
  if (!subtle || typeof subtle.digest !== 'function') {
    throw new Error('Web Crypto subtle.digest is unavailable');
  }
  const bytes = new TextEncoder().encode(value);
  const digest = await subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

export function fingerprint(value, length = 12) {
  if (typeof value !== 'string' || value.length === 0) return null;
  return value.slice(0, length);
}
