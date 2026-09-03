import { PROVIDERS } from './cookies.js';
import { HOST_RESULT_STATUSES, sanitizeHostResult } from './protocol.js';

export const DEFAULT_DEBOUNCE_MS = 2 * 60 * 1000;
export const PERIODIC_SYNC_MINUTES = 15;

const SAFE_RESULTS = new Set([
  ...HOST_RESULT_STATUSES,
  'permission_required',
  'missing_cookie',
  'paused',
]);

const SAFE_REASONS = new Set([
  'ok',
  'invalid_response',
  'native_host_unavailable',
  'permission_denied',
  'missing_cookie',
  'candidate_invalid',
  'upstream_temporary_failure',
  'server_rejected',
  'cookie_read_failed',
  'permission_check_failed',
  'malformed_cookie',
  'hashing_failed',
]);

export function createProviderState() {
  return {
    hash: null,
    lastSyncAt: null,
    lastResult: null,
    lastReason: null,
  };
}

export function createDefaultState() {
  return {
    enabled: true,
    providers: Object.fromEntries(PROVIDERS.map((provider) => [provider, createProviderState()])),
    lastUpdatedAt: null,
  };
}

function safeTimestamp(value) {
  return Number.isFinite(value) && value > 0 ? value : null;
}

function safeResult(value) {
  return typeof value === 'string' && SAFE_RESULTS.has(value) ? value : null;
}

function safeReason(value) {
  return typeof value === 'string' && SAFE_REASONS.has(value) ? value : null;
}

function sanitizeProviderResult(result, provider) {
  if (result && typeof result === 'object' && SAFE_RESULTS.has(result.status)) {
    const reason = safeReason(result.reason);
    return reason ? { status: result.status, reason } : { status: result.status };
  }
  return sanitizeHostResult(result, provider);
}

export function sanitizeState(raw) {
  const defaults = createDefaultState();
  if (!raw || typeof raw !== 'object') return defaults;
  const providers = {};
  for (const provider of PROVIDERS) {
    const candidate = raw.providers?.[provider];
    providers[provider] = {
      hash:
        typeof candidate?.hash === 'string' && /^[a-f0-9]{64}$/u.test(candidate.hash)
          ? candidate.hash
          : null,
      lastSyncAt: safeTimestamp(candidate?.lastSyncAt),
      lastResult: safeResult(candidate?.lastResult),
      lastReason: safeReason(candidate?.lastReason),
    };
  }
  return {
    enabled: raw.enabled !== false,
    providers,
    lastUpdatedAt: safeTimestamp(raw.lastUpdatedAt),
  };
}

/**
 * Update only metadata. The header itself is deliberately not accepted here,
 * so callers cannot accidentally put a Cookie into chrome.storage.local.
 */
export function updateProviderState(rawState, provider, metadata = {}) {
  if (!PROVIDERS.includes(provider)) {
    throw new Error(`unknown provider: ${provider}`);
  }
  const state = sanitizeState(rawState);
  const result = sanitizeProviderResult(metadata.result, provider);
  const providerState = state.providers[provider];
  if (typeof metadata.hash === 'string' && /^[a-f0-9]{64}$/u.test(metadata.hash)) {
    providerState.hash = metadata.hash;
  }
  providerState.lastSyncAt = safeTimestamp(metadata.now) ?? providerState.lastSyncAt;
  if (metadata.result !== undefined) {
    providerState.lastResult = result.status;
    providerState.lastReason = result.reason ?? null;
  }
  state.lastUpdatedAt = safeTimestamp(metadata.now) ?? state.lastUpdatedAt;
  return state;
}

export function updateLocalResult(rawState, provider, result, now) {
  return updateProviderState(rawState, provider, {
    result: { status: result },
    now,
  });
}

export function nextDebounceAt(now = Date.now(), delayMs = DEFAULT_DEBOUNCE_MS) {
  if (!Number.isFinite(now) || !Number.isFinite(delayMs) || delayMs < 0) {
    throw new TypeError('now and delayMs must be finite non-negative numbers');
  }
  return Math.floor(now + delayMs);
}

export function parseDebounceAlarm(name) {
  const prefix = 'rsshub-cookie-sync:debounce:';
  if (typeof name !== 'string' || !name.startsWith(prefix)) return null;
  const provider = name.slice(prefix.length);
  return PROVIDERS.includes(provider) ? provider : null;
}
