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

function makeChrome({ initialStorage = {}, deferStorageGet = false } = {}) {
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
      '刷新状态不得读取浏览器 Cookie',
    );
    assert.equal(
      fake.nativeMessages.length,
      beforeRefreshStatus.nativeMessages,
      '刷新状态不得上传 Cookie 或触发 Native Messaging',
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
