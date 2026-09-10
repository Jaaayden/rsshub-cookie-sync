# 从旧版本升级到 v1.2.0

适用于已经安装并能同步知乎、微博的用户。需要依次更新 **服务端 → Mac Native Host → Edge 扩展**；只替换扩展 ZIP 不够，旧 Host 和服务端不认识 `twitter` provider。

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

普通已完成迁移的重装不会无条件重建 RSSHub。只有检测到实际待完成的 Compose 迁移时，才执行初始化及对应健康检查。等待安装器报告成功后再继续。

## 2. 更新 Mac Native Host

回到 Mac 本机终端，以当前普通用户运行，**不要加 sudo**：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh
```

安装器会保留已有服务器地址、端口和项目专用密钥 `~/.ssh/rsshub-cookie-sync`。同一把公钥已在服务器授权时，不需要再次粘贴授权。

如果安装器提示旧配置使用 `id_ed25519` 或其他通用密钥，它会停止而不是替换密钥。请先按 [Native Host 的两阶段迁移说明](../native-host/README.md#旧版或通用密钥迁移) 操作；不要删除旧配置来绕过检查。

## 3. 更新 Edge 扩展

1. 下载 [v1.2.0 扩展 ZIP](https://github.com/Jaaayden/rsshub-cookie-sync/releases/download/v1.2.0/rsshub-cookie-sync-extension.zip)，解压到临时目录。
2. 在 `edge://extensions` 找到 RSSHub Cookie Sync，确认原来加载的扩展目录。用解压后包含 `manifest.json` 的目录内文件替换原扩展目录中的对应文件，避免多套一层目录。
3. 点击该扩展的“重新加载”，确认页面显示版本 **1.2.0**。不用移除再安装扩展；保留原目录和固定扩展 ID。
4. 打开扩展，进入“连接设置”，点击“重新读取设置”，确认 Native Host 可用，服务器地址、端口和密钥文件名仍正确。
5. 点击“授权站点权限”，授予新增的 `x.com` 和 `twitter.com` 权限。在同一个 Edge Default Profile 登录 [X](https://x.com)。
6. 点击“立即同步”。确认 X/Twitter 卡片出现结果后，恢复自动同步开关。

可从 [v1.2.0 Release](https://github.com/Jaaayden/rsshub-cookie-sync/releases/tag/v1.2.0) 下载 `SHA256SUMS`；将其与 ZIP 放在同一目录后运行：

```sh
grep ' rsshub-cookie-sync-extension.zip$' SHA256SUMS | shasum -a 256 -c -
```

应输出 `OK`。上面的两个安装命令始终安装最新稳定 Release；目前是 v1.2.0，以安装器显示的版本为准。以后有更新版本时，请使用同一版本的扩展及组件。

## 4. 确认升级结果

- “已切换”：新令牌已验证并写入 RSSHub。
- “候选已保存”：新令牌有效，但已有 live 令牌仍可用，所以暂不替换，属于正常结果。
- “已同步”：令牌与当前 live 或候选相同。
- “稍后重试”：上游验证、网络或连接暂时失败；查看 [故障排查](troubleshooting.md#xtwitter)，不要反复覆盖 live env。

需要检查服务器状态时，在 root shell 中运行：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json status --json
systemctl is-active rsshub-cookie-sync-monitor.timer
```

确认状态中出现 `providers.twitter`，且定时器返回 `active`。“刷新扩展状态”只刷新本地缓存；实际重新读取浏览器并验证需要点击“立即同步”。

未配置 X 时不会发送 X 登录失效提醒。已有逗号分隔的多账号令牌池会原样保留，状态显示 `twitter_token_pool_unsupported`；本版本不会自动用单个浏览器账号替换它。X 登录验证要求服务器直接访问 `x.com` 和 `api.x.com`，不继承环境代理；接口异常不会被当作登录成功。
