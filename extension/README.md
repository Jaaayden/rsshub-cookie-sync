# RSSHub Cookie Sync Edge 扩展

[返回项目总览](../README.md)

这是 Microsoft Edge Manifest V3 扩展。它读取当前 Edge Default Profile 中适用于知乎、微博实际请求地址的 Cookie，通过本机 Native Host 发送到你自己的 RSSHub 服务器。

服务器地址、SSH 私钥和 Bark 配置不放在扩展代码或扩展存储中。服务器地址、SSH 端口和要使用的 SSH 文件名，可以在扩展的“连接设置”页保存到本机 Native Host。

## 安装

### 使用 GitHub Release

从 [GitHub Releases](https://github.com/Jaaayden/rsshub-cookie-sync/releases) 下载 `rsshub-cookie-sync-extension.zip`，解压到固定目录。在 Edge 中打开 `edge://extensions`，打开“开发人员模式”，点击“加载解压缩的扩展”，选择解压后直接包含 `manifest.json` 的目录。

### 使用源码

```sh
git clone https://github.com/Jaaayden/rsshub-cookie-sync.git ~/rsshub-cookie-sync
```

然后在 `edge://extensions` 中加载 `~/rsshub-cookie-sync/extension`。不需要 `npm install`；Node.js 只用于测试。

官方扩展带有固定的公开 `key`，所以官方 Release 和源码目录的扩展 ID 一致。不要加载来历不明但复用了相同公开 key 的目录；固定 ID 不是签名。

## 第一次使用

1. 在同一个 Edge 配置文件中登录 `https://www.zhihu.com` 和 `https://m.weibo.cn`。
2. 打开扩展，点击“授权站点权限”，只授予知乎和微博的精确 host 权限。
3. 确认已经按 [Native Host 说明](../native-host/README.md) 安装本机桥接程序，并在扩展“连接设置”中填写服务器地址、端口和 SSH 密钥文件名。
4. 点击“立即同步”。

> 重要：普通安装应选择项目专用的 `rsshub-cookie-sync` 私钥，不会回退到 `id_ed25519` 等通用登录密钥。选择私钥不会自动授权；对应的 `~/.ssh/rsshub-cookie-sync.pub` 必须先通过管理员 SSH 连接安装到服务器的 `rsshub-sync` 账号。扩展日常连接使用的不是 `root`。

扩展只读取以下两个请求 URL 适用的 Cookie：

```text
https://www.zhihu.com/api/v3/moments
https://m.weibo.cn/feed/group
```

服务器会先验证候选。无效候选不会覆盖当前 live 配置；只有一方成功时，另一方也不会被清空。

## 按钮说明

### 立即同步

读取当前登录态并上传。Cookie 只在当前调用的内存和 Native Host 的 SSH 标准输入中短暂存在。

### 刷新扩展状态

重新向后台读取已经保存的脱敏结果、时间和权限状态，然后重新渲染弹窗。它不会调用 Cookie API、不会上传 Cookie、不会启动 SSH。

如果弹窗显示的是刚才的旧结果，点击“刷新扩展状态”；如果要重新读取网站登录态，点击“立即同步”。

### 复制 Cookie

知乎和微博卡片各有一个“复制 Cookie”按钮。只有在你明确点击某个 provider 的按钮、确认安全警告并授权剪贴板后，扩展才会读取该 provider 的 Cookie 并复制到系统剪贴板。

这是为应急手动替换保留的例外。复制后，以 root 登录 RSSHub 服务器并运行对应命令：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync manual-update --provider zhihu
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync manual-update --provider weibo
```

在隐藏提示中粘贴，不要把 Cookie 写进命令参数，也不要粘贴到聊天、Issue 或网页表单。服务端会继续执行验证、候选和回滚事务。扩展不会保存复制内容；剪贴板由操作系统管理，请在完成后清理。

### 自动同步开关

扩展启动时、每 15 分钟以及目标 Cookie 变化后会尝试同步。Cookie 变化会等待约 2 分钟合并。暂停只影响客户端自动采集，不会停止服务器定时监控。

## 连接设置

在弹窗点击“连接设置”：

- “服务器地址”：填写运行 RSSHub 的域名或 IPv4 地址；
- “SSH 端口”：通常为 `22`；
- “SSH 密钥文件名”：普通安装选择 `~/.ssh/rsshub-cookie-sync`，并确认它与服务器上的公钥是一对。
- “SSH 用户名”：只读显示为 `rsshub-sync`；它不可修改，也不是 `root`。

点击“保存连接设置”后，设置写入本机 Native Host 的 `config.json`，不会写入扩展存储。扩展只接触文件名，Native Host 只把选中的路径交给本机系统 SSH；私钥内容不会进入扩展。服务器端账号由安装器创建为 `rsshub-sync`，不需要在这里填写。

如果刚安装 Host，默认密钥名是 `rsshub-cookie-sync`；安装器只会创建或复用这一把项目专用密钥，不会自动采用 `id_ed25519` 等通用密钥。私钥旁边必须存在配对的 `.pub`，权限为 `0600` 或更严格，并且该公钥已经安装到服务器 `rsshub-sync` 账号。RSA、ECDSA 和缺少 `.pub` 的私钥不会出现在列表中。仅在下拉框中选择已有私钥不会完成授权。连接设置页会在保存后显示结果；“重新读取设置”可以再次从 Native Host 读取确认。

扩展允许高级用户手动选择 `~/.ssh/` 下其他已有的 Ed25519 私钥作为兼容入口，但安装器不会为这类密钥创建、替换或自动迁移配置。只有在确认该密钥没有同时用于 `root` 或其他服务器时才应使用；共享通用密钥会扩大 Cookie 同步链路泄露后的影响范围。

公钥授权

新手首次安装时不需要单独执行授权命令：先在 Mac 安装 Native Host 并复制公钥，再普通 SSH 登录服务器一次，在同一个 root shell 中运行服务端安装器，安装器要求粘贴公钥时直接粘贴即可。公钥只通过安装器标准输入传递，不会发送私钥。

如果首次安装时跳过了公钥，或需要更换密钥，请按 [Native Host 的高级 SSH 说明](../docs/advanced-ssh.md)中的单独 provision 和迁移流程操作。`provision-key` 每次会替换服务器上旧的同步公钥；更换后仍使用旧私钥的设备会无法连接。

如果 Native Host 提示发现旧版或通用密钥，请先按 [Native Host 的两阶段迁移说明](../native-host/README.md#旧版或通用密钥迁移) 执行 `--prepare-dedicated-key`、授权 `rsshub-cookie-sync.pub`，再执行 `--activate-dedicated-key`。普通重装不会静默替换旧密钥。

## 权限

Manifest 申请：

- `cookies`：读取两个目标请求的 Cookie；
- `alarms`：定时同步和合并 Cookie 变化；
- `nativeMessaging`：调用本机 Host；
- `storage`：只保存启用开关、SHA-256 指纹、时间和固定结果码；
- 可选 `clipboardWrite`：仅在用户明确执行“复制 Cookie”时使用；
- 可选 host permissions：知乎、微博四个精确 host。

没有全站点权限、网页脚本注入权限或 `clipboardRead` 权限。扩展不会跨浏览器或跨 Edge Profile 读取 Cookie。

## 隐私和安全

扩展不会保存 Cookie 原文、上游响应、服务器地址、SSH 私钥或 Bark key。Native Host 会把 Cookie 放入 SSH 标准输入，服务端只返回固定状态：

```text
unchanged
candidate_saved
promoted
rejected_invalid
retryable_error
```

浏览器关闭或 Mac 睡眠时无法采集，服务器端仍可继续检查 RSSHub 和 provider 登录态。扩展不会代替用户输入密码，也不会绕过验证码或 MFA。

## 更新和卸载

使用源码时，更新代码后在 `edge://extensions` 找到扩展并点击“重新加载”。使用 ZIP 时，下载新版本并加载新解压目录。只要 `manifest.json` 的固定 `key` 不变，扩展 ID 和 Native Host 授权不变。

卸载扩展不会删除服务器上的 RSSHub 或 live `rsshub.env`；服务端卸载会删除候选 Cookie 和 `/var/lib/rsshub-cookie-sync` 状态，并只删除确认由本项目创建的 `rsshub-sync` 账号。如果安装前已经存在同名账号，卸载只移除本项目添加的 SSH 授权和相关配置，不删除账号本身。完整顺序是“服务端 → Edge → Mac”：先卸载服务端，再在 `edge://extensions` 移除扩展，最后处理本机 Native Host。

已通过安装器安装过 Native Host 时，使用安装后的固定入口：

```sh
"$HOME/Library/Application Support/rsshub-cookie-sync/uninstall.sh"
```

如果固定入口不存在，也可以使用 macOS curl bootstrap：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh -s -- uninstall
```

两个入口都会默认保留 `~/.ssh/rsshub-cookie-sync`、对应 `.pub` 和 `~/.ssh/known_hosts`。如果是从源码目录安装、且固定入口不可用，才使用兼容命令 `python3 native-host/uninstall.py`；不要为卸载递归删除用户目录。服务端固定卸载命令、curl bootstrap 和 secret 保留边界见[项目总览](../README.md#卸载顺序)。

## 测试和打包

```sh
npm test
```

也可以在仓库根目录运行 `make check`。GitHub Actions 会在 Release 时生成 `rsshub-cookie-sync-extension.zip`、macOS 一键安装脚本 `install-macos.sh` 和服务端一键安装脚本 `install-server.sh`；CI 不是安装前提。
