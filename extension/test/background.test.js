import test from 'node:test';
import assert from 'node:assert/strict';

function makeEvent() {
  const listeners = [];
  return {
    listeners,
    addListener(listener) {
      listeners.push(listener);
    },
    dispatch(...args) {
      return listeners.map((listener) => listener(...args));
    },
  };
}

async function flush() {
  // The background code deliberately schedules all browser API work through
  // promises. A few turns also drain the serial state-write queue.
  for (let index = 0; index < 8; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function waitFor(predicate, message, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) {
      assert.fail(message);
    }
    // Web Crypto may finish on a worker thread, so counting a fixed number of
    // microtask/immediate turns is not a portable completion condition.
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  await flush();
}

function fakeCookie(provider) {
  if (provider === 'twitter') {
    return { name: 'auth_token', value: 'twitter-secret', domain: '.x.com', path: '/', secure: true, storeId: '0' };
  }
  if (provider === 'zhihu') {
    return {
      name: 'z_c0',
      value: 'zhihu-secret=a=b',
      domain: '.zhihu.com',
      path: '/',
      secure: true,
      hostOnly: false,
      storeId: '0',
    };
  }
  return {
    name: 'SUB',
    value: 'weibo-secret',
    domain: '.weibo.cn',
    path: '/',
    secure: true,
    hostOnly: false,
    storeId: '0',
  };
}

function makeChrome({ initialStorage = {}, deferStorageGet = false, nativeConfigResponse = null } = {}) {
  const events = {
    installed: makeEvent(),
    startup: makeEvent(),
    alarm: makeEvent(),
    cookieChanged: makeEvent(),
    message: makeEvent(),
  };
  const alarmCalls = [];
  const nativeMessages = [];
  const cookieReads = [];
  const permissionChecks = [];
  const storage = structuredClone(initialStorage);
  const pendingStorageGets = [];
  let permissionsGranted = true;

  const chrome = {
    runtime: {
      id: 'ohpnejcdmchhchkamammonikfbmfpiam',
      lastError: null,
      onInstalled: events.installed,
      onStartup: events.startup,
      onMessage: events.message,
      sendNativeMessage(hostName, payload, callback) {
        nativeMessages.push({ hostName, payload });
        if (payload.action === 'get-config') {
          callback(nativeConfigResponse ?? { ok: false, error: 'configuration_missing' });
          return;
        }
        if (payload.action === 'set-config') {
          callback(nativeConfigResponse ?? {
            status: 'config_saved',
            server: payload.server,
            identityName: payload.identityName,
            identities: [{ name: payload.identityName, legacy: false }],
          });
          return;
        }
        const providers = {};
        for (const provider of Object.keys(payload.providers ?? {})) {
          providers[provider] = { status: 'candidate_saved', reason: 'ok' };
        }
        callback({ version: 1, providers });
      },
    },
    storage: {
      local: {
        get(key, callback) {
          if (deferStorageGet) {
            pendingStorageGets.push({ key, callback });
          } else {
            callback({ [key]: storage[key] });
          }
        },
        set(values, callback) {
          Object.assign(storage, structuredClone(values));
          callback?.();
        },
      },
    },
    permissions: {
      contains(details, callback) {
        permissionChecks.push(structuredClone(details));
        callback(permissionsGranted);
      },
    },
    cookies: {
      onChanged: events.cookieChanged,
      getAll(details, callback) {
        cookieReads.push(structuredClone(details));
        const provider = details.url.includes('zhihu') ? 'zhihu' : details.url.includes('weibo') ? 'weibo' : 'twitter';
        callback([fakeCookie(provider)]);
      },
    },
    alarms: {
      onAlarm: events.alarm,
      create(name, details) {
        alarmCalls.push({ operation: 'create', name, details });
      },
      clear(name) {
        alarmCalls.push({ operation: 'clear', name });
        return Promise.resolve(true);
      },
    },
  };

  return {
    chrome,
    events,
    alarmCalls,
    nativeMessages,
    cookieReads,
    permissionChecks,
    storage,
    setPermissionsGranted(value) {
      permissionsGranted = value;
    },
    releaseStorageGets() {
      for (const { key, callback } of pendingStorageGets.splice(0)) {
        callback({ [key]: storage[key] });
      }
    },
  };
}

async function sendMessage(event, message) {
  let response;
  event.dispatch(message, {}, (value) => {
    response = value;
  });
  await flush();
  return response;
}

test('background lifecycle schedules install/startup/periodic/debounced sync safely', async () => {
  const previousChrome = globalThis.chrome;
  const fake = makeChrome();
  globalThis.chrome = fake.chrome;
  try {
    // A cache-busting query makes this test independent of any other dynamic
    // import a test runner may have performed in the same worker.
    await import(`../background.js?fake-chrome=${Date.now()}`);
    await flush();

    assert.ok(
      fake.alarmCalls.some(
        (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:periodic' && call.details.periodInMinutes === 15,
      ),
    );
    const initialMessages = fake.nativeMessages.length;
    fake.events.installed.dispatch({ reason: 'install' });
    await waitFor(
      () => fake.nativeMessages.length >= initialMessages + 3,
      'install should try all providers',
    );
    assert.ok(fake.nativeMessages.length >= initialMessages + 3, 'install should try all providers');
    assert.ok(
      fake.permissionChecks.some(
        ({ origins }) => origins.includes('https://zhihu.com/*') && origins.includes('https://www.zhihu.com/*'),
      ),
      'Zhihu permission check must cover both the cookie domain and request host',
    );
    assert.ok(
      fake.permissionChecks.some(
        ({ origins }) => origins.includes('https://weibo.cn/*') && origins.includes('https://m.weibo.cn/*'),
      ),
      'Weibo permission check must cover both the cookie domain and request host',
    );

    const afterInstall = fake.nativeMessages.length;
    fake.events.startup.dispatch();
    await waitFor(
      () => fake.nativeMessages.length >= afterInstall + 3,
      'startup should try all providers',
    );
    assert.ok(fake.nativeMessages.length >= afterInstall + 3, 'startup should try all providers');

    const afterStartup = fake.nativeMessages.length;
    fake.events.alarm.dispatch({ name: 'rsshub-cookie-sync:periodic' });
    await waitFor(
      () => fake.nativeMessages.length >= afterStartup + 3,
      'periodic alarm should try all providers',
    );
    assert.ok(fake.nativeMessages.length >= afterStartup + 3, 'periodic alarm should try all providers');

    const beforeDebounce = fake.nativeMessages.length;
    const debounceCreatesBeforeFirstEvent = fake.alarmCalls.filter(
      (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:debounce:zhihu',
    ).length;
    const zhihuEvent = { cookie: fakeCookie('zhihu'), removed: false };
    fake.events.cookieChanged.dispatch(zhihuEvent);
    await waitFor(
      () => fake.alarmCalls.filter(
        (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:debounce:zhihu',
      ).length === debounceCreatesBeforeFirstEvent + 1,
      'cookie change should schedule a Zhihu debounce alarm',
    );
    assert.equal(fake.nativeMessages.length, beforeDebounce, 'cookie change waits for debounce alarm');
    assert.ok(
      fake.alarmCalls.some(
        (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:debounce:zhihu' && Number.isFinite(call.details.when),
      ),
    );

    const debounceCreatesBeforeSecondEvent = fake.alarmCalls.filter(
      (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:debounce:zhihu',
    ).length;
    fake.events.cookieChanged.dispatch(zhihuEvent);
    await waitFor(
      () => fake.alarmCalls.filter(
        (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:debounce:zhihu',
      ).length === debounceCreatesBeforeSecondEvent + 1,
      'a second cookie change should replace the debounce alarm',
    );
    const debounceCreatesAfterSecondEvent = fake.alarmCalls.filter(
      (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:debounce:zhihu',
    ).length;
    assert.equal(debounceCreatesAfterSecondEvent, debounceCreatesBeforeSecondEvent + 1);
    assert.ok(
      fake.alarmCalls.some(
        (call) => call.operation === 'clear' && call.name === 'rsshub-cookie-sync:debounce:zhihu',
      ),
    );

    const beforeDebounceAlarm = fake.nativeMessages.length;
    fake.events.alarm.dispatch({ name: 'rsshub-cookie-sync:debounce:zhihu' });
    await waitFor(
      () => fake.nativeMessages.length === beforeDebounceAlarm + 1,
      'debounce alarm should sync only Zhihu',
    );
    assert.equal(fake.nativeMessages.length, beforeDebounceAlarm + 1, 'debounce alarm should sync only Zhihu');

    const statusResponse = await sendMessage(fake.events.message, {
      type: 'get-status',
    });
    assert.equal(statusResponse.ok, true);
    assert.equal(statusResponse.providers.zhihu.lastResult, 'candidate_saved');
    assert.equal(statusResponse.providers.weibo.lastResult, 'candidate_saved');
    assert.equal(statusResponse.providers.twitter.lastResult, 'candidate_saved');
    assert.equal(statusResponse.providers.zhihu.hash.length, 64);

    const beforeRefreshStatus = {
      cookieReads: fake.cookieReads.length,
      nativeMessages: fake.nativeMessages.length,
    };
    const refreshStatusResponse = await sendMessage(fake.events.message, {
      type: 'get-status',
    });
    assert.equal(refreshStatusResponse.ok, true);
    assert.equal(
      fake.cookieReads.length,
      beforeRefreshStatus.cookieReads,
      '刷新扩展状态不得读取浏览器 Cookie',
    );
    assert.equal(
      fake.nativeMessages.length,
      beforeRefreshStatus.nativeMessages,
      '刷新扩展状态不得上传 Cookie 或触发 Native Messaging',
    );

    const pausedResponse = await sendMessage(fake.events.message, {
      type: 'set-enabled',
      enabled: false,
    });
    assert.equal(pausedResponse.ok, true);
    assert.equal(pausedResponse.enabled, false);

    const beforePausedEvents = fake.nativeMessages.length;
    const debounceCreateCount = fake.alarmCalls.filter(
      (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:debounce:weibo',
    ).length;
    fake.events.cookieChanged.dispatch({ cookie: fakeCookie('weibo'), removed: false });
    fake.events.alarm.dispatch({ name: 'rsshub-cookie-sync:periodic' });
    await flush();
    assert.equal(fake.nativeMessages.length, beforePausedEvents, 'paused automation must not upload');
    assert.equal(
      fake.alarmCalls.filter(
        (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:debounce:weibo',
      ).length,
      debounceCreateCount,
      'paused automation must not schedule cookie debounce',
    );

    const stored = JSON.stringify(fake.storage);
    assert.equal(stored.includes('zhihu-secret'), false);
    assert.equal(stored.includes('weibo-secret'), false);
    assert.equal(stored.includes('twitter-secret'), false);
    assert.equal(stored.includes('cookieHeader'), false);
    assert.equal(stored.includes('cookieCount'), false);
  } finally {
    globalThis.chrome = previousChrome;
  }
});

test('冷启动时 Cookie 变化会先读取已暂停状态再决定是否安排同步', async () => {
  const previousChrome = globalThis.chrome;
  const fake = makeChrome({
    deferStorageGet: true,
    initialStorage: {
      rsshubCookieSyncState: {
        enabled: false,
        providers: {},
        lastUpdatedAt: null,
      },
    },
  });
  globalThis.chrome = fake.chrome;
  try {
    await import(`../background.js?cold-paused=${Date.now()}-${Math.random()}`);
    fake.events.cookieChanged.dispatch({ cookie: fakeCookie('zhihu'), removed: false });
    fake.releaseStorageGets();
    await flush();

    assert.equal(
      fake.alarmCalls.filter(
        (call) => call.operation === 'create' && call.name === 'rsshub-cookie-sync:debounce:zhihu',
      ).length,
      0,
    );
    assert.equal(fake.nativeMessages.length, 0);
  } finally {
    globalThis.chrome = previousChrome;
  }
});

test('复制 Cookie 只读取请求中的 provider，不上传、不持久化 Cookie', async () => {
  const previousChrome = globalThis.chrome;
  const fake = makeChrome();
  globalThis.chrome = fake.chrome;
  try {
    await import(`../background.js?copy-cookie=${Date.now()}-${Math.random()}`);
    await flush();

    const beforeReads = fake.cookieReads.length;
    const beforeNativeMessages = fake.nativeMessages.length;
    const response = await sendMessage(fake.events.message, {
      type: 'copy-cookie',
      provider: 'zhihu',
    });

    assert.deepEqual(response, {
      ok: true,
      provider: 'zhihu',
      cookieHeader: 'z_c0=zhihu-secret=a=b',
    });
    assert.equal(fake.cookieReads.length, beforeReads + 1);
    assert.equal(fake.cookieReads.at(-1).url, 'https://www.zhihu.com/api/v3/moments');
    assert.equal(fake.nativeMessages.length, beforeNativeMessages);

    const invalid = await sendMessage(fake.events.message, {
      type: 'copy-cookie',
      provider: 'not-a-provider',
    });
    assert.deepEqual(invalid, { ok: false, error: 'invalid_provider' });
    assert.equal(fake.cookieReads.length, beforeReads + 1, '未知服务不得读取 Cookie');

    const stored = JSON.stringify(fake.storage);
    assert.equal(stored.includes('zhihu-secret'), false);
    assert.equal(stored.includes('cookieHeader'), false);
  } finally {
    globalThis.chrome = previousChrome;
  }
});

test('复制 Cookie 的站点权限被拒绝时不读取目标 Cookie', async () => {
  const previousChrome = globalThis.chrome;
  const fake = makeChrome();
  fake.setPermissionsGranted(false);
  globalThis.chrome = fake.chrome;
  try {
    await import(`../background.js?copy-cookie-denied=${Date.now()}-${Math.random()}`);
    await flush();

    const response = await sendMessage(fake.events.message, {
      type: 'copy-cookie',
      provider: 'weibo',
    });
    assert.deepEqual(response, { ok: false, error: 'permission_required' });
    assert.equal(fake.cookieReads.length, 0);
    assert.equal(fake.nativeMessages.length, 0);
  } finally {
    globalThis.chrome = previousChrome;
  }
});

test('连接设置通过 Native Host 控制消息读写，刷新不读取或上传 Cookie', async () => {
  const previousChrome = globalThis.chrome;
  const config = {
    host: 'rsshub.example.test',
    port: 2222,
    user: 'rsshub-sync',
    identityName: 'rsshub-cookie-sync',
  };
  const fake = makeChrome({
    nativeConfigResponse: {
      status: 'config',
      server: {
        host: config.host,
        port: config.port,
        user: config.user,
      },
      identityName: config.identityName,
      identities: [
        { name: config.identityName, legacy: false },
        { name: 'another-key', legacy: false },
      ],
      cookieHeader: 'secret=must-not-reach-extension-state',
    },
  });
  globalThis.chrome = fake.chrome;
  try {
    await import(`../background.js?native-config=${Date.now()}-${Math.random()}`);
    await flush();
    const before = {
      cookieReads: fake.cookieReads.length,
      storage: JSON.stringify(fake.storage),
    };

    const loaded = await sendMessage(fake.events.message, { type: 'get-native-config' });
    assert.deepEqual(loaded, {
      ok: true,
      config,
      identities: [
        { name: config.identityName, legacy: false },
        { name: 'another-key', legacy: false },
      ],
    });
    assert.deepEqual(fake.nativeMessages.at(-1).payload, {
      version: 1,
      action: 'get-config',
    });
    assert.equal(fake.cookieReads.length, before.cookieReads);
    assert.equal(JSON.stringify(fake.storage), before.storage);

    const saved = await sendMessage(fake.events.message, {
      type: 'set-native-config',
      config,
    });
    assert.deepEqual(saved, loaded);
    assert.deepEqual(fake.nativeMessages.at(-1).payload, {
      version: 1,
      action: 'set-config',
      server: {
        host: config.host,
        port: config.port,
        user: config.user,
      },
      identityName: config.identityName,
    });
    assert.equal(fake.cookieReads.length, before.cookieReads);
    assert.equal(JSON.stringify(fake.storage), before.storage);
  } finally {
    globalThis.chrome = previousChrome;
  }
});

test('连接设置输入非法时不会启动 Native Host', async () => {
  const previousChrome = globalThis.chrome;
  const fake = makeChrome();
  globalThis.chrome = fake.chrome;
  try {
    await import(`../background.js?native-config-invalid=${Date.now()}-${Math.random()}`);
    await flush();
    const before = fake.nativeMessages.length;
    const response = await sendMessage(fake.events.message, {
      type: 'set-native-config',
      config: {
        host: 'rsshub.example.test;bad',
        port: 22,
        user: 'rsshub-sync',
        identityName: 'rsshub-cookie-sync',
      },
    });
    assert.deepEqual(response, { ok: false, error: 'configuration_invalid' });
    assert.equal(fake.nativeMessages.length, before);
  } finally {
    globalThis.chrome = previousChrome;
  }
});

test('Twitter prefers X, falls back without mixing, debounces auth_token and never stores credentials', async () => {
  const previousChrome = globalThis.chrome;
  const fake = makeChrome();
  const row = (domain, value) => ({ name: 'auth_token', domain, value, path: '/', secure: true });
  let xRows = [row('.x.com', 'x-secret')];
  fake.chrome.cookies.getAll = (details, callback) => {
    fake.cookieReads.push(details);
    callback(details.url === 'https://x.com/' ? xRows : [row('.twitter.com', 'legacy-secret')]);
  };
  globalThis.chrome = fake.chrome;
  try {
    await import(`../background.js?twitter=${Date.now()}-${Math.random()}`);
    await flush();
    let response = await sendMessage(fake.events.message, { type: 'copy-cookie', provider: 'twitter' });
    assert.equal(response.cookieHeader, 'auth_token=x-secret');
    assert.deepEqual(fake.cookieReads.map((r) => r.url), ['https://x.com/']);
    xRows = [];
    response = await sendMessage(fake.events.message, { type: 'copy-cookie', provider: 'twitter' });
    assert.equal(response.cookieHeader, 'auth_token=legacy-secret');
    assert.deepEqual(fake.cookieReads.slice(-2).map((r) => r.url), ['https://x.com/', 'https://twitter.com/']);
    xRows = [row('.x.com', 'one'), row('.x.com', 'two')];
    const before = fake.cookieReads.length;
    response = await sendMessage(fake.events.message, { type: 'copy-cookie', provider: 'twitter' });
    assert.equal(response.ok, false);
    assert.equal(fake.cookieReads.length, before + 1, 'ambiguous X must not silently use another account');
    fake.setPermissionsGranted(false);
    response = await sendMessage(fake.events.message, { type: 'copy-cookie', provider: 'twitter' });
    assert.equal(response.error, 'permission_required');
    assert.equal(fake.cookieReads.length, before + 1);
    fake.setPermissionsGranted(true);
    xRows = [row('.x.com', 'x-secret')];
    const alarm = 'rsshub-cookie-sync:debounce:twitter';
    fake.events.cookieChanged.dispatch({ cookie: row('.twitter.com', 'legacy-secret'), removed: true });
    await waitFor(() => fake.alarmCalls.some((c) => c.name === alarm && c.operation === 'create'), 'Twitter change schedules debounce');
    assert.equal(fake.nativeMessages.length, 0);
    fake.events.alarm.dispatch({ name: alarm });
    await waitFor(() => fake.nativeMessages.length === 1, 'Twitter debounce uploads once');
    assert.deepEqual(fake.nativeMessages[0].payload.providers, { twitter: { cookieHeader: 'auth_token=x-secret' } });
    assert.equal(JSON.stringify(fake.storage).includes('x-secret'), false);
    assert.equal(JSON.stringify(fake.storage).includes('legacy-secret'), false);
    assert.equal(JSON.stringify(fake.storage).includes('cookieHeader'), false);
    assert.equal(fake.storage.rsshubCookieSyncState.providers.twitter.hash.length, 64);
    const creates = fake.alarmCalls.length;
    fake.events.cookieChanged.dispatch({ cookie: { ...row('.x.com', 'csrf'), name: 'ct0' } });
    await flush();
    assert.equal(fake.alarmCalls.length, creates);
  } finally {
    globalThis.chrome = previousChrome;
  }
});

test('missing Twitter auth_token reads both domains but never uploads an empty credential', async () => {
  const previousChrome = globalThis.chrome;
  const fake = makeChrome();
  fake.chrome.cookies.getAll = (details, callback) => {
    fake.cookieReads.push(details);
    callback([{ name: 'ct0', value: 'csrf-only', domain: new URL(details.url).hostname, path: '/' }]);
  };
  globalThis.chrome = fake.chrome;
  try {
    await import(`../background.js?twitter-missing=${Date.now()}-${Math.random()}`);
    await flush();
    fake.events.alarm.dispatch({ name: 'rsshub-cookie-sync:debounce:twitter' });
    await flush();
    assert.deepEqual(fake.cookieReads.map((row) => row.url), ['https://x.com/', 'https://twitter.com/']);
    assert.equal(fake.nativeMessages.length, 0);
    const status = await sendMessage(fake.events.message, { type: 'get-status' });
    assert.equal(status.providers.twitter.lastResult, 'missing_cookie');
    assert.equal(JSON.stringify(fake.storage).includes('csrf-only'), false);
  } finally {
    globalThis.chrome = previousChrome;
  }
});

test('single-provider retry preserves other providers and logs safe per-stage results', async () => {
  const previousChrome = globalThis.chrome;
  const fake = makeChrome({ initialStorage: { rsshubCookieSyncState: { enabled: false, providers: { weibo: { lastResult: 'unchanged', lastSyncAt: 100 } } } } });
  fake.chrome.runtime.sendNativeMessage = (hostName, payload, callback) => {
    fake.nativeMessages.push({ hostName, payload });
    callback({ status: 'retryable_error', reason: 'twitter_csrf_missing', cookieHeader: 'LEAK-ME' });
  };
  globalThis.chrome = fake.chrome;
  try {
    await import(`../background.js?retry-single=${Date.now()}-${Math.random()}`);
    await flush();
    const response = await sendMessage(fake.events.message, { type: 'sync-provider', provider: 'twitter' });
    assert.equal(response.ok, true);
    assert.equal(fake.nativeMessages.length, 1);
    assert.deepEqual(Object.keys(fake.nativeMessages[0].payload.providers), ['twitter']);
    assert.equal(fake.nativeMessages[0].payload.diagnostics, true);
    assert.deepEqual(fake.cookieReads.map((r) => r.url), ['https://x.com/']);
    assert.equal(response.providers.weibo.lastSyncAt, 100);
    assert.equal(response.providers.twitter.lastReason, 'twitter_csrf_missing');
    const logs = await sendMessage(fake.events.message, { type: 'get-diagnostics' });
    assert.deepEqual(logs.entries.map((e) => e.stage), ['collect', 'native', 'complete']);
    assert.equal(logs.entries.at(-1).reason, 'twitter_csrf_missing');
    assert.equal(JSON.stringify(fake.storage).includes('LEAK-ME'), false);
    assert.equal(JSON.stringify(fake.storage).includes('twitter-secret'), false);
    const reads = fake.cookieReads.length;
    await sendMessage(fake.events.message, { type: 'get-diagnostics' });
    await sendMessage(fake.events.message, { type: 'clear-diagnostics' });
    assert.equal(fake.cookieReads.length, reads);
    assert.deepEqual((await sendMessage(fake.events.message, { type: 'get-diagnostics' })).entries, []);
    const invalid = await sendMessage(fake.events.message, { type: 'sync-provider', provider: 'evil' });
    assert.equal(invalid.ok, false);
    assert.equal(fake.nativeMessages.length, 1);
  } finally { globalThis.chrome = previousChrome; }
});
