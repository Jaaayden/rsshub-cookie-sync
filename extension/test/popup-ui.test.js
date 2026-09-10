import test from 'node:test';
import assert from 'node:assert/strict';

class Element {
  constructor() { this.children = []; this.events = {}; this.textContent = ''; this.disabled = false; this.open = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  addEventListener(name, fn) { this.events[name] = fn; }
}
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };

test('popup renders independent retry controls and safely shows diagnostic entries', async () => {
  const previous = { document: globalThis.document, chrome: globalThis.chrome };
  const ids = Object.fromEntries(['enabled', 'grant', 'refresh', 'sync', 'settings', 'notice', 'providers', 'diagnostics', 'diagnostic-output', 'diagnostic-notice', 'diagnostic-refresh', 'diagnostic-clear', 'diagnostic-export'].map((id) => [`#${id}`, new Element()]));
  const messages = [];
  const response = { ok: true, enabled: true, permissions: { zhihu: true, weibo: true, twitter: true }, providers: { zhihu: { lastResult: 'retryable_error' }, weibo: { lastResult: 'unchanged' }, twitter: { lastResult: 'retryable_error', lastReason: 'twitter_csrf_missing' } } };
  globalThis.document = { querySelector: (id) => ids[id], createElement: () => new Element(), querySelectorAll: () => [] };
  globalThis.chrome = { runtime: { lastError: null, sendMessage: (message, callback) => {
    messages.push(message);
    callback(message.type === 'get-diagnostics' ? { ok: true, entries: [{ time: 1000, provider: 'twitter', stage: 'complete', status: 'retryable_error', reason: 'twitter_csrf_missing', cookie: 'SECRET' }] } : response);
  } } };
  try {
    await import(`../popup.js?ui=${Date.now()}`);
    await flush();
    assert.equal(ids['#providers'].children.length, 3);
    const twitter = ids['#providers'].children[2];
    assert.match(twitter.children[3].textContent, /CSRF/);
    const retry = twitter.children.at(-1).children[0];
    assert.equal(retry.textContent, '重试此站点');
    retry.events.click();
    await flush();
    assert.deepEqual(messages.filter((m) => m.type.startsWith('sync')), [{ type: 'sync-provider', provider: 'twitter' }]);
    assert.doesNotMatch(ids['#notice'].textContent, /同步完成|SSH|公钥/);
    ids['#diagnostics'].open = true;
    ids['#diagnostics'].events.toggle({ target: ids['#diagnostics'] });
    await flush();
    assert.match(ids['#diagnostic-output'].textContent, /CSRF/);
    assert.doesNotMatch(ids['#diagnostic-output'].textContent, /SECRET/);
  } finally { globalThis.document = previous.document; globalThis.chrome = previous.chrome; }
});
