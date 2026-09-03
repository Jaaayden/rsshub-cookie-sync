import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const extensionDir = dirname(dirname(fileURLToPath(import.meta.url)));

test('连接设置页已注册为 Manifest options_ui，并包含刷新/保存控件', async () => {
  const manifest = JSON.parse(await readFile(join(extensionDir, 'manifest.json'), 'utf8'));
  assert.deepEqual(manifest.options_ui, { page: 'options.html', open_in_tab: true });
  const html = await readFile(join(extensionDir, 'options.html'), 'utf8');
  assert.match(html, /id="refresh"/);
  assert.match(html, /id="settings-form"/);
  assert.match(html, /id="identity-name"/);
  assert.match(html, /id="server-user"[^>]*value="rsshub-sync"[^>]*readonly/);
  assert.match(html, /选择私钥不会自动授权/);
  assert.match(html, /Ed25519/);
});

test('连接设置页刷新和加载只走脱敏控制消息，不读取 Cookie 或扩展存储', async () => {
  const source = await readFile(join(extensionDir, 'options.js'), 'utf8');
  const html = await readFile(join(extensionDir, 'options.html'), 'utf8');
  assert.match(source, /type: 'get-native-config'/);
  assert.match(source, /type: 'set-native-config'/);
  assert.doesNotMatch(source, /createSetNativeConfigMessage\(config\)\.config/);
  assert.doesNotMatch(source, /chrome\.cookies|chrome\.storage|cookies\.getAll|storage\.local/);
  assert.match(html, /重新读取设置/);
});

test('Popup 提供连接设置入口', async () => {
  const html = await readFile(join(extensionDir, 'popup.html'), 'utf8');
  const source = await readFile(join(extensionDir, 'popup.js'), 'utf8');
  assert.match(html, /id="settings"/);
  assert.match(source, /settingsButton\.addEventListener\('click'/);
  assert.match(source, /runtime\.openOptionsPage\(\)/);
  assert.match(source, /\.pub 公钥安装到服务器的 rsshub-sync 账号/);
});
