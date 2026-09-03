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
        const provider = details.url.includes('zhihu') ? 'zhihu' : 'weibo';
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
      () => fake.nativeMessages.length >= initialMessages + 2,
      'install should try both providers',
    );
    assert.ok(fake.nativeMessages.length >= initialMessages + 2, 'install should try both providers');
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
      () => fake.nativeMessages.length >= afterInstall + 2,
      'startup should try both providers',
    );
    assert.ok(fake.nativeMessages.length >= afterInstall + 2, 'startup should try both providers');

    const afterStartup = fake.nativeMessages.length;
    fake.events.alarm.dispatch({ name: 'rsshub-cookie-sync:periodic' });
    await waitFor(
      () => fake.nativeMessages.length >= afterStartup + 2,
      'periodic alarm should try both providers',
    );
    assert.ok(fake.nativeMessages.length >= afterStartup + 2, 'periodic alarm should try both providers');

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
