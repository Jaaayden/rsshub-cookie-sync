import { PROVIDERS, validateCookieHeader } from './cookies.js';

export const PROTOCOL_VERSION = 1;
export const NATIVE_HOST_NAME = 'com.jayden.rsshub_cookie_sync';

export const HOST_RESULT_STATUSES = Object.freeze([
  'unchanged',
  'candidate_saved',
  'promoted',
  'rejected_invalid',
  'retryable_error',
]);

const STATUS_SET = new Set(HOST_RESULT_STATUSES);

export function createSyncPayload(providerHeaders) {
  if (!providerHeaders || typeof providerHeaders !== 'object') {
    throw new TypeError('providerHeaders must be an object');
  }

  const providers = {};
  for (const provider of PROVIDERS) {
    const header = providerHeaders[provider];
    if (header === undefined) continue;
    validateCookieHeader(header);
    providers[provider] = { cookieHeader: header };
  }
  if (Object.keys(providers).length === 0) {
    throw new TypeError('at least one provider is required');
  }
  return { version: PROTOCOL_VERSION, providers };
}

export function isKnownHostStatus(status) {
  return typeof status === 'string' && STATUS_SET.has(status);
}

function sanitizeReason(reason) {
  const allowed = new Set([
    'ok',
    'invalid_response',
    'native_host_unavailable',
    'permission_denied',
    'missing_cookie',
    'candidate_invalid',
    'upstream_temporary_failure',
    'server_rejected',
  ]);
  return allowed.has(reason) ? reason : undefined;
}

/**
 * Only copy the finite protocol vocabulary into extension storage/UI. A
 * native host must never be able to make arbitrary text (which could contain
 * a Cookie or response body) persistent.
 */
export function sanitizeHostResult(result, provider) {
  const candidate =
    result && typeof result === 'object' && result.providers && provider
      ? result.providers[provider]
      : result;
  if (!candidate || typeof candidate !== 'object') {
    return { status: 'retryable_error', reason: 'invalid_response' };
  }
  const status = isKnownHostStatus(candidate.status)
    ? candidate.status
    : 'retryable_error';
  const reason = sanitizeReason(candidate.reason);
  if (!isKnownHostStatus(candidate.status)) {
    return { status, reason: 'invalid_response' };
  }
  return reason ? { status, reason } : { status };
}
