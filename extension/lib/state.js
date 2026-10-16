import { PROVIDERS } from './cookies.js';
import {
  HOST_REASONS,
  HOST_RESULT_STATUSES,
  LOCAL_REASONS,
  sanitizeHostResult,
  sanitizeReason,
} from './protocol.js';

export const DEFAULT_DEBOUNCE_MS = 2 * 60 * 1000;
export const PERIODIC_SYNC_MINUTES = 15;
// A trailing debounce that is restarted by every change would never fire while
// cookies keep changing.  Cap the total wait; the periodic sync is the fallback.
export const MAX_DEBOUNCE_WAIT_MS = 10 * 60 * 1000;
// A periodic or cookie-change check whose Cookie is byte-identical to the one
// the server already confirmed is not uploaded again, but the server is still
// refreshed this often so a re-installed or restored server heals itself.
export const UPLOAD_REFRESH_MS = 6 * 60 * 60 * 1000;
const CONFIRMED_RESULTS = new Set(['unchanged', 'promoted']);

const SAFE_RESULTS = new Set([
  ...HOST_RESULT_STATUSES,
  'permission_required',
  'missing_cookie',
  'paused',
]);

const SAFE_REASONS = new Set([...HOST_REASONS, ...LOCAL_REASONS]);

export function createProviderState() {
  return {
    hash: null,
    lastSyncAt: null,
    lastUploadAt: null,
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
  return sanitizeReason(value) ?? (typeof value === 'string' && SAFE_REASONS.has(value) ? value : null);
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
      lastUploadAt: safeTimestamp(candidate?.lastUploadAt),
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
  if (metadata.result !== undefined && CONFIRMED_RESULTS.has(result.status)) {
    // The server just confirmed this Cookie; remember when.
    providerState.lastUploadAt = safeTimestamp(metadata.now) ?? providerState.lastUploadAt;
  }
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

/**
 * When a debounced sync should fire. Each change pushes the alarm back by
 * `delayMs`, but never later than `maxWaitMs` after the first change in the
 * current burst.
 */
export function debounceFireAt(now, firstChangeAt, delayMs = DEFAULT_DEBOUNCE_MS, maxWaitMs = MAX_DEBOUNCE_WAIT_MS) {
  const first = Number.isFinite(firstChangeAt) ? firstChangeAt : now;
  return Math.floor(Math.max(now, Math.min(now + delayMs, first + maxWaitMs)));
}

/**
 * True when an automatic check can skip the upload: the Cookie hash equals the
 * one the server last confirmed, that confirmation is recent, and the trigger
 * is a periodic or cookie-change check. Start-up, enabling and manual syncs
 * always upload.
 */
export function canSkipUpload(providerState, hash, reason, now) {
  const automatic = reason === 'periodic' || (typeof reason === 'string' && reason.startsWith('cookie_changed:'));
  if (!automatic || !providerState || typeof hash !== 'string') return false;
  if (providerState.hash !== hash || !CONFIRMED_RESULTS.has(providerState.lastResult)) return false;
  const age = now - providerState.lastUploadAt;
  return Number.isFinite(age) && age >= 0 && age < UPLOAD_REFRESH_MS;
}
