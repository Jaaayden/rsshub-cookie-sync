# 故障排查

先按下面的顺序判断问题在哪一段：

```text
Edge 权限 → Native Host → SSH 公钥/主机密钥 → 服务器探针 → RSSHub 容器
```

排查时不要把 Cookie、Bark Key、SSH 私钥、完整请求头或未脱敏日志发到网上。

## 扩展状态旧或按钮没有反应

“刷新扩展状态”只读取扩展后台已经保存的状态，不会重新读取 Cookie。弹窗打开很久后先点击它；要重新采集当前登录态，再点击“立即同步”。

如果扩展更新过，在 `edge://extensions` 点击“重新加载”，再重新打开弹窗。确认扩展没有显示错误，并且 Edge 使用的是 Default Profile。

## 显示“需授权”或“未找到 Cookie”

点击“授权站点权限”，允许知乎和微博的精确站点权限。然后确认：

- 知乎使用 `www.zhihu.com` 登录；
- 微博使用 `m.weibo.cn` 移动站登录；
- 登录发生在当前 Edge Profile；
- 站点没有刚刚注销或触发重新登录。

重新登录后点击“立即同步”。扩展不会代替输入密码，也不会绕过验证码或 MFA。

## Native Host 不可用

在 Mac 终端重新运行：

```sh
cd ~/rsshub-cookie-sync
python3 native-host/install.py
```

然后在 `edge://extensions` 点击扩展“重新加载”。检查以下两点：

- Native Messaging manifest 的文件名为 `com.jayden.rsshub_cookie_sync.json`；
- `allowed_origins` 中的扩展 ID 与 Edge 页面显示的 ID 完全一致。

如果使用自定义扩展，必须在安装时传入该扩展 ID；官方扩展不需要传参数。

## SSH 连接失败

打开扩展“连接设置”，确认服务器地址、SSH 端口和密钥文件名。然后确认：

1. 选中的私钥位于当前用户的 `~/.ssh/`，并且与服务器上 provision 的 `.pub` 公钥是一对；
2. `~/.ssh/known_hosts` 中存在目标地址和端口的 Ed25519 条目；
3. 条目的指纹已经通过服务器控制台或其他独立可信渠道核对；
4. 服务器端 `rsshub-sync` 账号仍存在，公钥没有被替换或删除。

主机密钥校验故意采用严格模式。不要使用“接受未知 key”或关闭校验来绕过错误。

## `候选被拒绝`

这表示知乎或微博的登录态探针明确失败。无效候选不会覆盖 live env，也不会重建 RSSHub。请在 Edge 重新登录对应 provider，再点“立即同步”。

如果自动链路暂时不可用，也可以在扩展中复制对应 Cookie，再在服务器运行 `rsshub-cookie-sync manual-update --provider zhihu` 或 `--provider weibo` 的完整安装路径命令。具体命令见[项目首页的手动应急更新](../README.md#手动应急更新-cookie)。不要直接编辑 `rsshub.env`，否则会绕过验证和回滚事务。

## `稍后重试`、超时或上游错误

`403`、`429`、`432`、超时和 `5xx` 会被视为临时上游或网络故障，不会立即替换 Cookie。等待下一次定时检查，或确认服务器 DNS、出口网络、防火墙和上游限流情况。

如果只是某个 provider 出错，另一方的有效 live Cookie 不会被清空。

## 查看服务器状态

只查看脱敏状态：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json status --json
```

查看监控 timer 和日志：

```sh
systemctl status rsshub-cookie-sync-monitor.timer
systemctl list-timers rsshub-cookie-sync-monitor.timer
journalctl -u rsshub-cookie-sync-monitor.service -n 100 --no-pager
```

日志只应包含状态码、分类原因和时间，不应包含 Cookie、请求头、Bark Key 或完整上游响应。

## Bark 没有通知

在服务器上重新配置并测试：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json configure-bark

/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json notify-test
```

输入 Device Key 时不会回显。Bark 故障不会阻止 Cookie 验证和自动切换。

## Compose 或容器异常

不要手动删除 `secrets/rsshub.env`、事务文件或状态目录。安装器和服务端事务会在下次运行时尝试恢复。

先确认 Docker 服务和目标 RSSHub 容器仍在运行，再查看服务端日志和脱敏状态。只有在需要处理非标准 Compose 布局时，才阅读 [Compose 高级说明](advanced-compose.md)；普通安装不需要手工指定 project 或 service。

## 安装失败或需要回滚

安装器遇到校验、健康检查或事务失败时会停止启用新的监控配置，并尝试恢复旧文件。保留错误发生时的脱敏终端信息，检查：

- Docker Compose 是否为 v2.30+；
- Compose 文件和父目录是否由 root 管理且不可由其他用户写入；
- RSSHub service 是否真实存在；
- 服务器本机 `http://127.0.0.1:1200/healthz` 是否可访问。

不要把不带 `--quiet` 的 Compose 展开输出复制到公开场所，因为其中可能包含环境变量。

## 仍然无法解决

提交问题时只提供：软件版本、操作系统、脱敏状态字段、HTTP 状态码和分类原因。请先阅读 [安全策略](../SECURITY.md)，不要提供 Cookie、Bark Key、私钥、完整 Compose 或真实请求头。
