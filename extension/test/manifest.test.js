import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const extensionDir = dirname(dirname(fileURLToPath(import.meta.url)));

test('manifest requests only the intended APIs and exact optional cookie-domain hosts', async () => {
  const manifest = JSON.parse(await readFile(join(extensionDir, 'manifest.json'), 'utf8'));
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual([...manifest.permissions].sort(), [
    'alarms',
    'cookies',
    'nativeMessaging',
    'storage',
  ]);
  assert.deepEqual([...manifest.optional_host_permissions].sort(), [
    'https://m.weibo.cn/*',
    'https://weibo.cn/*',
    'https://www.zhihu.com/*',
    'https://zhihu.com/*',
  ]);
  assert.equal(manifest.optional_host_permissions.some((origin) => origin.includes('*.')), false);
  assert.equal(manifest.optional_host_permissions.includes('<all_urls>'), false);
  assert.equal(Object.hasOwn(manifest, 'host_permissions'), false);
  assert.equal(manifest.background.service_worker, 'background.js');
  assert.equal(manifest.background.type, 'module');
  assert.equal(manifest.key.length > 100, true);
});
