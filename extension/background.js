import {
  COOKIE_TARGETS,
  PROVIDERS,
  applicableCookies,
  serializeCookieHeader,
  sha256Hex,
} from './lib/cookies.js';
import {
  NATIVE_HOST_NAME,
  createSyncPayload,
  sanitizeHostResult,
} from './lib/protocol.js';
import {
  DEFAULT_DEBOUNCE_MS,
  PERIODIC_SYNC_MINUTES,
  createDefaultState,
  nextDebounceAt,
  parseDebounceAlarm,
  sanitizeState,
  updateProviderState,
} from './lib/state.js';

const STORAGE_KEY = 'rsshubCookieSyncState';
const PERIODIC_ALARM = 'rsshub-cookie-sync:periodic';
const DEBOUNCE_PREFIX = 'rsshub-cookie-sync:debounce:';

let state = createDefaultState();
let stateLoaded = false;
let stateWriteQueue = Promise.resolve();
let syncQueue = Promise.resolve();

class ExtensionApiError extends Error {
  constructor(code = 'api_error') {
    super(code);
    this.name = 'ExtensionApiError';
    this.code = code;
  }
}

function chromeContext() {
  return globalThis.chrome;
}

/**
 * Call a Chrome API that returns a value. Edge currently supports the Promise
 * forms of these APIs, while the callback path keeps the extension usable on
 * older Chromium builds as well. Errors are intentionally normalized so an
 * exception cannot leak request data into storage or the console.
 */
function callChrome(fn, context, ...args) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      callback(value);
    };
    const callback = (value) => {
      const runtime = chromeContext()?.runtime;
      if (runtime?.lastError) {
        finish(reject, new ExtensionApiError('api_error'));
        return;
      }
      finish(resolve, value);
    };

    let returned;
    try {
      returned = fn.apply(context, [...args, callback]);
    } catch {
      finish(reject, new ExtensionApiError('api_error'));
      return;
    }
    if (returned && typeof returned.then === 'function') {
      returned.then(
        (value) => finish(resolve, value),
        () => finish(reject, new ExtensionApiError('api_error')),
      );
    }
  });
}

/** Invoke an API where no result is needed (e.g. alarms.create). */
async function invokeChrome(fn, context, ...args) {
  try {
    const returned = fn.apply(context, args);
    if (returned && typeof returned.then === 'function') {
      await returned;
    }
  } catch {
    throw new ExtensionApiError('api_error');
  }
}

function safeLog(event) {
  // Keep diagnostics fixed-vocabulary and argument-free: Cookie API objects,
  // API errors and native-host responses must never reach the console.
  console.info(`[RSSHub Cookie Sync] ${event}`);
}

async function loadState() {
  if (stateLoaded) return state;
  try {
    const result = await callChrome(
      chromeContext().storage.local.get,
      chromeContext().storage.local,
      STORAGE_KEY,
    );
    state = sanitizeState(result?.[STORAGE_KEY]);
  } catch {
    state = createDefaultState();
    safeLog('state_unavailable');
  }
  stateLoaded = true;
  return state;
}

function persistState(nextState) {
  state = sanitizeState(nextState);
  const snapshot = state;
  stateWriteQueue = stateWriteQueue
    .catch(() => undefined)
    .then(async () => {
      try {
        await callChrome(
          chromeContext().storage.local.set,
          chromeContext().storage.local,
          { [STORAGE_KEY]: snapshot },
        );
      } catch {
        safeLog('state_write_failed');
      }
    });
  return stateWriteQueue;
}

async function recordProvider(provider, metadata) {
  const nextState = updateProviderState(state, provider, metadata);
  await persistState(nextState);
}

function permissionOriginsFor(provider) {
  return COOKIE_TARGETS[provider].permissionOrigins;
}

async function hasProviderPermission(provider) {
  try {
    const result = await callChrome(
      chromeContext().permissions.contains,
      chromeContext().permissions,
      { origins: permissionOriginsFor(provider) },
    );
    return result === true;
  } catch {
    throw new ExtensionApiError('permission_check_failed');
  }
}

async function collectProvider(provider) {
  let granted;
  try {
    granted = await hasProviderPermission(provider);
  } catch {
    return { error: 'permission_check_failed' };
  }
  if (!granted) return { error: 'permission_required' };

  let records;
  try {
    records = await callChrome(
      chromeContext().cookies.getAll,
      chromeContext().cookies,
      { url: COOKIE_TARGETS[provider].url },
    );
  } catch {
    return { error: 'cookie_read_failed' };
  }

  const selected = applicableCookies(provider, records);
  if (selected.length === 0) return { error: 'missing_cookie' };
  let header;
  try {
    header = serializeCookieHeader(selected);
  } catch {
    return { error: 'malformed_cookie' };
  }
  if (!header) return { error: 'missing_cookie' };
  return { header };
}

async function sendNativePayload(payload) {
  return callChrome(
    chromeContext().runtime.sendNativeMessage,
    chromeContext().runtime,
    NATIVE_HOST_NAME,
    payload,
  );
}

function localFailureResult(reason) {
  if (reason === 'permission_required') {
    return { status: 'permission_required', reason: 'permission_denied' };
  }
  if (reason === 'missing_cookie') {
    return { status: 'missing_cookie', reason: 'missing_cookie' };
  }
  return { status: 'retryable_error', reason };
}

async function performProviderSync(provider, reason) {
  const now = Date.now();
  const collection = await collectProvider(provider);
  if (collection.error) {
    await recordProvider(provider, {
      result: localFailureResult(collection.error),
      now,
    });
    return { status: collection.error };
  }

  let hash;
  try {
    hash = await sha256Hex(collection.header);
  } catch {
    await recordProvider(provider, {
      result: { status: 'retryable_error', reason: 'hashing_failed' },
      now,
    });
    return { status: 'hashing_failed' };
  }

  let response;
  try {
    const payload = createSyncPayload({ [provider]: collection.header });
    response = await sendNativePayload(payload);
  } catch {
    await recordProvider(provider, {
      hash,
      result: { status: 'retryable_error', reason: 'native_host_unavailable' },
      now,
    });
    return { status: 'native_host_unavailable' };
  }

  const result = sanitizeHostResult(response, provider);
  await recordProvider(provider, {
    hash,
    result,
    now,
  });
  return { status: result.status, reason: result.reason, reasonSource: reason };
}

function enqueueSync(task) {
  const run = syncQueue.then(task, task);
  syncQueue = run.catch(() => undefined);
  return run;
}

function automaticSync(reason, providers = PROVIDERS) {
  return enqueueSync(async () => {
    await loadState();
    if (!state.enabled) return { status: 'paused' };
    const result = {};
    for (const provider of providers) {
      result[provider] = await performProviderSync(provider, reason);
    }
    return result;
  });
}

function manualSync() {
  return enqueueSync(async () => {
    await loadState();
    const result = {};
    for (const provider of PROVIDERS) {
      result[provider] = await performProviderSync(provider, 'manual');
    }
    return result;
  });
}

async function ensurePeriodicAlarm() {
  try {
    await invokeChrome(
      chromeContext().alarms.create,
      chromeContext().alarms,
      PERIODIC_ALARM,
      { periodInMinutes: PERIODIC_SYNC_MINUTES },
    );
  } catch {
    safeLog('alarm_setup_failed');
  }
}

async function scheduleDebounce(provider) {
  const alarmName = `${DEBOUNCE_PREFIX}${provider}`;
  try {
    await invokeChrome(
      chromeContext().alarms.clear,
      chromeContext().alarms,
      alarmName,
    );
    await invokeChrome(
      chromeContext().alarms.create,
      chromeContext().alarms,
      alarmName,
      { when: nextDebounceAt(Date.now(), DEFAULT_DEBOUNCE_MS) },
    );
  } catch {
    safeLog('debounce_setup_failed');
  }
}

async function getPermissionsStatus() {
  const entries = await Promise.all(
    PROVIDERS.map(async (provider) => {
      try {
        return [provider, await hasProviderPermission(provider)];
      } catch {
        return [provider, false];
      }
    }),
  );
  return Object.fromEntries(entries);
}

async function getStatus() {
  await loadState();
  return {
    enabled: state.enabled,
    permissions: await getPermissionsStatus(),
    providers: state.providers,
  };
}

async function setEnabled(enabled) {
  await loadState();
  const nextState = { ...state, enabled: enabled === true };
  await persistState(nextState);
  if (nextState.enabled) {
    void automaticSync('enabled');
  }
  return getStatus();
}

async function handleMessage(message) {
  if (!message || typeof message.type !== 'string') {
    return { ok: false, error: 'invalid_request' };
  }
  switch (message.type) {
    case 'get-status':
      return { ok: true, ...(await getStatus()) };
    case 'sync-now':
      await manualSync();
      return { ok: true, ...(await getStatus()) };
    case 'set-enabled':
      return { ok: true, ...(await setEnabled(message.enabled)) };
    default:
      return { ok: false, error: 'unsupported_request' };
  }
}

function registerListeners() {
  const browser = chromeContext();
  if (!browser?.runtime || !browser.alarms || !browser.cookies) return;

  browser.runtime.onInstalled?.addListener(() => {
    void ensurePeriodicAlarm().then(() => automaticSync('installed'));
  });
  browser.runtime.onStartup?.addListener(() => {
    void ensurePeriodicAlarm();
    void automaticSync('startup');
  });
  browser.alarms.onAlarm?.addListener((alarm) => {
    if (!alarm || typeof alarm.name !== 'string') return;
    if (alarm.name === PERIODIC_ALARM) {
      void automaticSync('periodic');
      return;
    }
    const provider = parseDebounceAlarm(alarm.name);
    if (provider) void automaticSync(`cookie_changed:${provider}`, [provider]);
  });
  browser.cookies.onChanged?.addListener((changeInfo) => {
    void (async () => {
      await loadState();
      if (!state.enabled || !changeInfo?.cookie || typeof changeInfo.cookie !== 'object') return;
      for (const provider of PROVIDERS) {
        try {
          if (applicableCookies(provider, [changeInfo.cookie]).length > 0) {
            await scheduleDebounce(provider);
          }
        } catch {
          // A malformed event is ignored. It is never logged with its contents.
        }
      }
    })();
  });
  browser.runtime.onMessage?.addListener((message, _sender, sendResponse) => {
    void handleMessage(message).then(
      (response) => sendResponse(response),
      () => sendResponse({ ok: false, error: 'internal_error' }),
    );
    return true;
  });
}

registerListeners();
void loadState().then(ensurePeriodicAlarm);
