# 从旧版本升级到 v1.4.1

## 这个版本改了什么

- **重装不再重复询问。** 服务端安装器会先沿用上次安装的 Compose 文件，不再先问当前目录里的其他项目；已配置 Bark 时只提示“已检测到 Bark 通知配置”，并询问是否更换设备密钥（默认不更换，换失败时原配置不变），不会再要求重新输入。v1.4.1 起，`status --json` 增加 `notifications.bark_configured`（仅布尔值，不含密钥）。
- **Native Host 改名。** 注册名称从 `com.jayden.rsshub_cookie_sync` 改为 `io.github.jaaayden.rsshub_cookie_sync`。新版 Mac 安装器会同时保留旧名称的 manifest，所以**先更新 Mac Native Host，旧扩展照常可用**；但新扩展只认新名称，**不能先于 Native Host 更新**。
- **安装器会校验下载内容。** 一键安装脚本现在下载 Release 的源码包和 `SHA256SUMS`，校验通过才会解压运行；校验失败时不会安装任何内容。
- **服务端代码改为 Python 包。** 入口脚本 `rsshub_cookie_sync.py` 的路径、命令和 systemd 配置都没变，旁边多了一个 `rsshub_sync/` 目录；服务端安装器会整体替换它，失败时整体回滚。
- **扩展更稳、更省。** Cookie 持续变化时最多等待 10 分钟再同步；Cookie 与服务器最近一次确认的完全相同时，周期检查不再经 SSH 重复上传（启动、手动同步和超过 6 小时仍会上传）；修复了 service worker 被唤醒时可能用旧状态覆盖新状态的竞态。

## 按当前版本选择要更新什么

| 当前版本 | 服务端 | Mac Native Host | Edge 扩展 |
| --- | --- | --- | --- |
| v1.3.x | 更新 | **更新（必须在扩展之前）** | 更新并重新加载 |
| v1.2.x 及更旧 | 更新 | 更新 | 更新，并授予新增站点权限 |

始终按 **服务端 → Mac Native Host → Edge 扩展** 的顺序操作。如果只想先试用而不动扩展，更新服务端和 Native Host 后旧扩展依旧正常工作。更早版本新增的功能（直接同步、诊断、订阅监控、X/Twitter、`ACCESS_KEY` 自动发现、非 Secure Cookie 的 HTTP host 权限）背景见各版本的 Release Notes；订阅监控需要额外配置路径，见 [配置说明](route-monitoring.md)。

无需先卸载。保留已有配置、SSH 密钥、站点登录态和服务器 `secrets/rsshub.env`。

## 1. 更新服务器程序

先在扩展弹窗关闭自动同步开关，等正在进行的同步结束。然后使用原来的管理员 SSH 连接服务器，例如：

```sh
ssh -p <服务器SSH端口> root@<服务器地址>
```

在服务器的 root shell 中运行：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh
```

- 确认安装器检测到的是原来的 Compose 文件；未找到时输入原文件的绝对路径。
- 普通官方布局沿用默认选项。自定义过 service、project 或健康地址的部署，应继续使用原参数，参阅 [Compose 高级说明](advanced-compose.md)，不要用更换部署的选项绕过配置不匹配。
- 已存在有效的本项目受限公钥时，安装器会保留它，不需要重新生成或替换公钥。如果旧安装没有有效授权，按提示提供当前 Mac 同步密钥的 `.pub` 公钥。
- 已有 Bark 配置会保留；不需要更改时，在“现在配置 Bark 通知吗”处直接回车选择否。

安装器会从所选 Compose service 的有效环境配置读取 `ACCESS_KEY`，安全保存供服务健康检查使用，不会显示密钥。普通已完成迁移的重装不会无条件重建 RSSHub。服务端安装器还会把 `/usr/local/lib/rsshub-cookie-sync/` 下新增的 `rsshub_sync/` 目录整体替换为新版，失败时与其他文件一起回滚。只有检测到实际待完成的 Compose 迁移时，才执行初始化及对应健康检查。等待安装器报告成功后再继续。

## 2. 更新 Mac Native Host（所有旧版本都需要）

回到 Mac 本机终端，以当前普通用户运行，**不要加 sudo**：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh
```

安装器会保留已有服务器地址、端口和项目专用密钥 `~/.ssh/rsshub-cookie-sync`。同一把公钥已在服务器授权时，不需要再次粘贴授权。完成后 `~/Library/Application Support/Microsoft Edge/NativeMessagingHosts/` 下会有新旧两个 manifest，这是预期的。

如果安装器提示旧配置使用 `id_ed25519` 或其他通用密钥，它会停止而不是替换密钥。请先按 [Native Host 的两阶段迁移说明](../native-host/README.md#旧版或通用密钥迁移) 操作；不要删除旧配置来绕过检查。

## 3. 更新 Edge 扩展

1. 下载 [v1.4.1 扩展 ZIP](https://github.com/Jaaayden/rsshub-cookie-sync/releases/download/v1.4.1/rsshub-cookie-sync-extension.zip)，解压到临时目录。
2. 在 `edge://extensions` 找到 RSSHub Cookie Sync，确认原来加载的扩展目录。用解压后包含 `manifest.json` 的目录内文件替换原扩展目录中的对应文件，避免多套一层目录。
3. 点击该扩展的“重新加载”，确认页面显示版本 **1.4.1**。不用移除再安装扩展；保留原目录和固定扩展 ID。
4. 打开扩展，进入“连接设置”，点击“重新读取设置”，确认 Native Host 可用，服务器地址、端口和密钥文件名仍正确。如果提示 Native Host 不可用，说明第 2 步还没做或没成功：先完成第 2 步，再回来重新加载扩展。
5. 点击“授权站点权限”，允许知乎和微博新增的精确 HTTP host 权限。Chromium 会根据 Cookie 的 `Secure` 属性检查对应 host 权限，因此这一步用于读取 `d_c0` 等非 Secure Cookie；扩展仍通过 HTTPS URL 查询 Cookie，不会用 HTTP 请求知乎或微博。升级旧于 v1.3.0 的版本时，也授予新增的 `x.com` 和 `twitter.com` 权限，并在同一个 Edge Default Profile 登录 [X](https://x.com)。
6. 点击“立即同步”。确认知乎、微博（以及已配置 X 时的 X/Twitter）卡片出现结果后，恢复自动同步开关。

可从 [v1.4.1 Release](https://github.com/Jaaayden/rsshub-cookie-sync/releases/tag/v1.4.1) 下载 `SHA256SUMS`；将其与 ZIP 放在同一目录后运行：

```sh
grep ' rsshub-cookie-sync-extension.zip$' SHA256SUMS | shasum -a 256 -c -
```

应输出 `OK`。上面的两个安装命令始终安装最新稳定 Release，并在解压前校验下载内容；以安装器显示的版本为准。Release 资产同时带有 GitHub 构建来源证明，可用 `gh attestation verify rsshub-cookie-sync-extension.zip --repo Jaaayden/rsshub-cookie-sync` 核对。

### 刷新 RSSHub `ACCESS_KEY` 配置

RSSHub 的 Compose 配置发生变化后，先按平常方式应用配置并确认目标 RSSHub 已使用新设置运行，再在服务器 root shell 中预览健康检查：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync refresh-rsshub-config --dry-run
```

预览通过后更新同步器配置：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync refresh-rsshub-config
```

如本地 RSSHub 地址或端口有变化，可同时指定新地址，例如 `--rsshub-base-url http://127.0.0.1:1300`。命令从当前所选 Compose service 重新读取 `ACCESS_KEY`，验证成功后才会原子写入配置；不会输出密钥、重建 RSSHub 或删除事务文件。`--dry-run` 仅进行只读 Docker/HTTP 查询且不写配置。完整说明见[服务端配置刷新](../server/README.md#刷新-rsshub-连接配置)。

## 4. 确认升级结果

- “已切换”：新凭证已写入 RSSHub；默认直接同步模式未验证上游登录态。
- “候选已保存”：仅显式使用 `verified` 模式时表示已保存通过探针的候选，暂不替换已有 live。
- “已同步”：令牌与当前 live 或候选相同。
- “稍后重试”：凭证不完整、连接或处理失败；查看卡片中的具体原因及[故障排查](troubleshooting.md)，不要反复覆盖 live env。

需要检查服务器状态时，在 root shell 中运行：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json status --json
systemctl is-active rsshub-cookie-sync-monitor.timer
```

确认状态中出现 `providers.twitter`，且定时器返回 `active`。“刷新扩展状态”只刷新本地缓存；实际重新读取浏览器并上传需要点击“立即同步”。完成同步后检查实际订阅路由，确认 RSSHub 已能抓取内容。

未配置 X 时不会发送 X 登录失效提醒。已有逗号分隔的多账号令牌池会原样保留，状态显示 `twitter_token_pool_unsupported`；本版本不会自动用单个浏览器账号替换它。仅显式使用 `verified` 模式时，X 登录验证要求服务器直接访问 `x.com` 和 `api.x.com`，不继承环境代理；接口异常不会被当作登录成功。
