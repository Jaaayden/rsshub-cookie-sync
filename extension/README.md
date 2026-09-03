# RSSHub Cookie Sync Edge 扩展

[返回项目总览](../README.md)

这是 RSSHub Cookie Sync 的 Microsoft Edge Manifest V3 采集端。扩展只读取当前 Edge 配置文件中适用于知乎和微博两个实际请求地址的 Cookie，通过 Native Messaging 交给本机 Host；Cookie 原文不会写入 `chrome.storage`、剪贴板、扩展日志或页面。

服务器地址、SSH 私钥、代理和 Bark 配置都不在扩展中。它们由本机 Native Host 和服务器端配置管理，适合每个用户连接自己的 RSSHub 实例。

## 安装方式

### 方式一：安装 Release ZIP

从项目的 [GitHub Releases](https://github.com/Jaaayden/rsshub-cookie-sync/releases) 下载扩展 ZIP，解压到一个固定目录。不要直接选择 ZIP 文件，也不要在解压后移动 `manifest.json`：加载目录的根部必须直接包含 `manifest.json`。

在 Edge 中：

1. 打开 `edge://extensions`；
2. 开启“开发人员模式”；
3. 点击“加载解压缩的扩展”；
4. 选择刚才解压出的、包含 `manifest.json` 的目录；
5. 记录页面显示的扩展 ID，后续安装 Native Host 时使用同一个值。

Release ZIP 只是方便分发，不是必须的构建产物。它不包含服务器密钥、Bark key、SSH 私钥或 Cookie。

### 方式二：直接加载源码

源码本身没有运行时构建依赖，可以直接在 Edge 中加载 `extension/` 目录：

```sh
git clone https://github.com/Jaaayden/rsshub-cookie-sync.git
```

然后在 `edge://extensions` 中按上面的步骤选择仓库内的 `extension/`。不需要 `npm install`；Node.js 只用于开发测试。

## 扩展 ID 与 Native Host

`manifest.json` 中的 `key` 是公开 RSA 公钥，用于固定扩展 ID，不是密码。当前官方源码和 Release ZIP 在不修改 manifest 的情况下使用同一个 ID。Native Host 安装器的 `--extension-id` 必须填写 Edge 页面实际显示的 32 位小写 a-p ID；不要凭记忆输入，也不要把 ID 当作秘密。

这个公开 key 只提供稳定 ID，不是扩展签名，也不能证明某个解压目录来自本项目。加载前请从本仓库或 GitHub Release 获取文件并核对来源；不要加载来历不明、复用了相同公开 key 的扩展。`allowed_origins` 只按扩展 ID 授权，无法区分两个使用相同公开 key 的解压扩展。

如果你删除或修改 `manifest.json` 的 `key`、自行重新打包或创建了自己的扩展 key，扩展 ID 会改变。此时必须把新的 ID 重新传给 Native Host 安装器，使 Native Host manifest 的 `allowed_origins` 与之匹配。不要把任何扩展私钥提交到仓库。

Native Host 的安装、服务器地址、SSH 公钥、`known_hosts` 和可选 SOCKS5 代理见 [Native Messaging Host 文档](../native-host/README.md)。服务端账号、公钥接入和 Compose 迁移见 [服务端文档](../server/README.md)。

## 首次使用

1. 在同一个 Edge 配置文件中登录 `www.zhihu.com` 和 `m.weibo.cn`；不需要把 Cookie 手工复制到任何文本文件。
2. 打开扩展弹窗，点击“授权站点权限”。扩展只请求知乎、微博四个精确主机的可选权限：

   ```text
   https://zhihu.com/*
   https://www.zhihu.com/*
   https://weibo.cn/*
   https://m.weibo.cn/*
   ```

3. 确认 Native Host 已安装并且服务器端已经接收本机公钥。
4. 点击“立即同步”。服务端会验证候选 Cookie；全新 RSSHub 会把首个有效候选提升为 live 配置，已有实例则按当前状态保存候选或保持不变。

扩展不会自动输入账号密码，也不会绕过验证码或 MFA。若站点登录态已失效，请先在 Edge 中重新登录，再点击“立即同步”。

## “刷新状态”和“立即同步”的区别

弹窗有三个相关操作：

- “刷新状态”：只向扩展后台读取已经保存的状态和当前站点权限，再重新渲染弹窗。它不会调用 `cookies.getAll`，不会启动 Native Messaging，也不会上传 Cookie；因此它是只读操作，适合查看后台最新结果。
- “立即同步”：读取两个目标请求地址的适用 Cookie，生成内存中的 Cookie header，通过 Native Host 上传。它会更新本地的状态指纹和结果。
- 右上角开关：暂停或恢复自动同步。暂停只影响启动、定时和 Cookie 变化触发的同步，仍允许手动“立即同步”。

“刷新状态”不会主动查询服务器 `status --json`；如果服务器刚由 timer 自动切换，扩展弹窗只有在下一次同步或后台收到相应结果后才会显示变化。需要查看服务端真实状态，请在服务器运行 `status --json`。

## 自动同步行为

- Edge 启动和扩展安装时会安排一次同步；
- 周期 alarm 每 15 分钟触发；
- 适用 Cookie 发生变化后，针对对应 provider 等待约 2 分钟再同步；短时间内多次变化会合并；
- Edge 关闭或 Mac 睡眠时不会采集，服务器端 systemd monitor 仍会继续检查登录态并通过 Bark 告警；
- 知乎采集 URL 为 `https://www.zhihu.com/api/v3/moments`，微博采集 URL 为 `https://m.weibo.cn/feed/group`。不同 host、端口或路径的 Cookie 不会被选入。

扩展只会把以下脱敏元数据写入 `chrome.storage.local`：启用开关、SHA-256 指纹、最后同步时间、固定结果和固定原因码。它不会保存 Cookie header、上游响应或 SSH/Bark 信息。

## 权限说明

Manifest 申请的权限是 `cookies`、`alarms`、`nativeMessaging` 和 `storage`；知乎、微博的 host permissions 为可选权限，必须由用户点击授权。没有 `all_urls`、网页脚本注入权限或剪贴板权限。

Cookie API 会根据目标请求的域、路径和 Secure 属性过滤记录。扩展同时申请根域和实际请求 host，是为了覆盖浏览器实际会发送的父域 Cookie 与 host-only Cookie；这不代表会读取其他网站的 Cookie。

## 状态与隐私

扩展 UI 只显示 provider 名、权限、时间、截短的哈希指纹和固定状态/原因，例如“候选已保存”“候选被拒绝”“Native Host 不可用”。未知的 Host 响应字段和错误正文会被丢弃。

Cookie 在浏览器 API 返回后只在当前同步调用的内存中存在，随后通过本机 Native Host 的 stdin 进入 SSH。Cookie 不会进入命令行参数、环境变量、扩展持久化存储、剪贴板或日志。拥有当前用户会话或调试权限的本机恶意程序仍可能攻击浏览器运行环境，因此请保护操作系统账户和 Edge 配置文件。

## 升级

如果使用源码加载：拉取新版本后，在 `edge://extensions` 找到扩展并点击“重新加载”。如果使用 Release ZIP：下载并解压新版本，再在扩展页移除旧目录后重新加载新目录，或按 Edge 页面提示更新路径。

只要保留 manifest 的固定 `key`，升级前后扩展 ID 不变，Native Host 的 `allowed_origins` 不需要重新配置。若扩展 ID 发生变化，请重新运行 Native Host 安装器并将新 ID 传给 `--extension-id`。

升级后建议依次点击“刷新状态”和“立即同步”，确认权限、Native Host 和服务器响应正常。

## 卸载

在 `edge://extensions` 对该扩展点击“移除”。如不再使用桥接程序，再按照 [Native Host 文档](../native-host/README.md) 执行 `python3 uninstall.py`。扩展卸载不会删除服务器上的 RSSHub、secret env、候选或状态；服务端也可以在没有 Edge 的情况下继续监控和使用最后一次 live 配置。

## 本地测试与打包

在 `extension/` 目录运行：

```sh
npm test
```

测试使用 Node.js 内置 test runner，覆盖 URL/域路径匹配、Cookie 序列化、重复 Cookie、状态脱敏、变化合并、Native 响应过滤和刷新按钮的只读行为。仓库根目录的 `make check` 会同时执行扩展、Native Host 和服务端测试。

无需 CI 也可以直接加载源码；GitHub Actions 只负责在推送或发布时运行检查并生成 ZIP，不能替代 Native Host 和服务器配置。

## 已知限制

- 采集端面向 Microsoft Edge Chromium；本项目提供的 Native Host 安装脚本是 macOS 用户级流程；
- 默认使用安装扩展的 Edge 配置文件，不会跨浏览器或跨 profile 读取 Cookie；
- 必须由用户在 Edge 中完成登录；不自动处理密码、验证码、MFA 或风控挑战；
- 服务器端判断的是登录态探针，不把 RSS 长时间没有新文章当作 Cookie 失效；
- Edge 关闭、设备睡眠、网络不可用或 Native Host 未安装时，客户端同步会延迟或失败，但不会把 Cookie 原文写入错误信息。
