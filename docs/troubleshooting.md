# 故障排查

v1.3.0 默认直接同步，上游 HTTP 404 或非 JSON 不再阻止上传。订阅监控的 `route_http_*` / `route_invalid_feed` 表示实际路由异常，不等同于 Cookie 失效。配置与状态检查见 [订阅监控](route-monitoring.md)。旧版独立探针诊断仅适用于 `verified` 模式。

先按下面的顺序判断问题在哪一段：

```text
Edge 权限 → Native Host → SSH 公钥/主机密钥 → 服务端格式检查与写入 → RSSHub 路由
```

排查时不要把 Cookie、Bark Key、SSH 私钥、完整请求头或未脱敏日志发到网上。

## 扩展状态旧或按钮没有反应

“刷新扩展状态”只读取扩展后台已经保存的状态，不会重新读取 Cookie。弹窗打开很久后先点击它；要重新采集当前登录态，再点击“立即同步”。

如果扩展更新过，在 `edge://extensions` 点击“重新加载”，再重新打开弹窗。确认扩展没有显示错误，并且 Edge 使用的是 Default Profile。

## 显示“需授权”或“未找到 Cookie”

点击“授权站点权限”，允许知乎、微博和 X/Twitter 的精确站点权限。然后确认：

- 知乎使用 `www.zhihu.com` 登录；
- 微博使用 `m.weibo.cn` 移动站登录；
- X/Twitter 优先在 `x.com` 登录，旧 `twitter.com` Cookie 仅作为无 X 令牌时的回退；
- 登录发生在当前 Edge Profile；
- 站点没有刚刚注销或触发重新登录。

重新登录后点击“立即同步”。扩展不会代替输入密码，也不会绕过验证码或 MFA。

更新扩展后请再次点击“授权站点权限”，并允许知乎、微博新增的 HTTP host 权限。Chromium 会按 Cookie 的 `Secure` 属性构造权限校验地址，HTTPS 权限不足以读取 `d_c0` 这类非 Secure Cookie；扩展实际仍只通过 HTTPS URL 查询 Cookie，不会用 HTTP 请求站点。[Chromium `cookies_helpers`](https://chromium.googlesource.com/chromium/src/+/f068a76f1819af40b5b5077fbcadc2381c1671d7/chrome/browser/extensions/api/cookies/cookies_helpers.h) 对此有实现注释。

## 知乎动态仍返回 403

确认当前 Edge 配置文件已登录知乎，并且扩展已更新、重新授权站点权限。知乎动态需要同一会话中的非空 `d_c0` 和 `z_c0`；缺失或存在冲突值时，扩展会在本机停止同步，不上传、不覆盖服务器上的 Cookie。同步上传会省略静态 `__zse_ck`，由 RSSHub 按当前 `d_c0` 会话生成；手动“复制 Cookie”仍保留浏览器返回的完整内容。RSSHub 的[知乎修复 PR #22319](https://github.com/DIYgod/RSSHub/pull/22319)说明了活动内容对 `z_c0` 的需要及自动生成 `__zse_ck` 的推荐配置。

默认 `direct` 模式不会验证上传后的知乎登录态，所以扩展显示同步成功不等于 RSSHub 已能抓取内容。完成同步后，检查实际订阅路由的 RSSHub 响应；订阅监控的 `route_http_403` 等结果代表路由请求异常，排查方式见[订阅监控](route-monitoring.md)。

## Native Host 不可用

普通安装在 Mac 终端重新运行本机 bootstrap：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh
```

如果你是从源码目录安装或正在调试，才参考 [Native Host 说明](../native-host/README.md) 中的源码兼容命令；普通安装不需要 clone 仓库或进入源码目录。

然后在 `edge://extensions` 点击扩展“重新加载”。检查以下两点：

- Native Messaging manifest 的文件名为 `com.jayden.rsshub_cookie_sync.json`；
- `allowed_origins` 中的扩展 ID 与 Edge 页面显示的 ID 完全一致。

如果使用自定义扩展，必须在安装时传入该扩展 ID；官方扩展不需要传参数。

## SSH 连接失败

打开扩展“连接设置”，确认服务器地址、SSH 端口和密钥文件名。然后确认：

1. 扩展日常连接的用户名固定为 `rsshub-sync`，不是 `root`；`root` 只用于服务端安装和公钥授权；
2. 普通安装选中的是项目专用私钥 `~/.ssh/rsshub-cookie-sync`，并且与服务器上 provision 的 `rsshub-cookie-sync.pub` 公钥是一对；
3. `~/.ssh/known_hosts` 中存在与目标地址和端口匹配的 OpenSSH 主机记录；
4. 条目的指纹已经通过服务器控制台或其他独立可信渠道核对；
5. 服务器端 `rsshub-sync` 账号仍存在，公钥没有被替换或删除。

最常见的原因是“选择了 `rsshub-cookie-sync`，但没有授权对应的 `rsshub-cookie-sync.pub`”，或者重新 provision 后扩展仍然选择了旧密钥。先在本机确认项目专用公钥指纹：

```sh
chmod 600 ~/.ssh/rsshub-cookie-sync
ssh-keygen -lf ~/.ssh/rsshub-cookie-sync.pub -E sha256
```

如果 `.pub` 文件不存在，可以在本机从项目专用私钥派生到同名位置，再查看指纹；放在其他目录不会让它出现在扩展密钥列表中：

```sh
ssh-keygen -y -f ~/.ssh/rsshub-cookie-sync > ~/.ssh/rsshub-cookie-sync.pub
chmod 644 ~/.ssh/rsshub-cookie-sync.pub
ssh-keygen -lf ~/.ssh/rsshub-cookie-sync.pub -E sha256
```

新手首次安装时，应从 Mac 普通 SSH 登录服务器一次，让 SSH 在确认提示中写入主机条目：

```sh
ssh -p <服务器SSH端口> root@<服务器地址>
```

确认连接目标正确后输入 `yes`，然后保持 root shell，在同一个会话重新运行服务端安装器；当它要求粘贴 Native Host 公钥时，粘贴 `~/.ssh/rsshub-cookie-sync.pub` 的整行内容。不要在新手流程中单独执行 `provision-key`。主机指纹的独立核对和单独 provision 只适用于[高级 SSH 说明](advanced-ssh.md)。

### 旧版配置或通用密钥

如果 `python3 native-host/install.py` 提示检测到旧版或通用 SSH 密钥，说明当前 Native Host 配置仍指向 `id_ed25519` 或其他非项目专用密钥。普通重装会在替换任何本机文件前停止，也不会偷偷改写配置。请先完成下面的两阶段迁移：

```sh
# 阶段 1：只创建/检查项目专用密钥，不修改旧配置
python3 native-host/install.py --prepare-dedicated-key

# 先用管理员账号授权新公钥
ssh -p <管理员SSH端口> root@<服务器地址> \
  /usr/local/sbin/rsshub-cookie-sync-provision-key \
  < ~/.ssh/rsshub-cookie-sync.pub

# 阶段 2：明确切换到项目专用密钥
python3 native-host/install.py --activate-dedicated-key
```

第二阶段会保留旧配置中的服务器地址和端口，并先用不含 Cookie 的空请求做一次 SSH 认证探测。只有专用公钥和 `known_hosts` 都验证通过，才会替换本机配置；失败时本机旧配置仍保持不变。完成后在扩展“连接设置”选择 `rsshub-cookie-sync` 并保存，再点击“立即同步”。服务器 provision 会立即替换旧同步公钥；如果激活失败，需要用管理员连接重新 provision 旧公钥，才能恢复旧连接。

扩展设置页可以手动选择其他已有 Ed25519 作为高级兼容入口，但不建议使用可能同时登录 `root` 或其他服务器的通用密钥。安装器不会为这类密钥创建或自动迁移配置。

主机密钥校验故意采用严格模式。不要使用“接受未知 key”或关闭校验来绕过错误。

## 服务端返回 `rejected_invalid`

默认 `direct` 模式不调用 provider 登录态探针；`rejected_invalid` 通常表示凭证格式检查未通过，或当前 X 多账号令牌池不受支持。只有显式设置 `sync_mode: "verified"` 时，这个结果才可能表示上游登录态探针拒绝了凭证。direct 模式细节见[同步模式与订阅监控](route-monitoring.md)。

如果自动链路暂时不可用，也可以在扩展中复制对应 Cookie，再在服务器运行 `rsshub-cookie-sync manual-update --provider zhihu` 或 `--provider weibo` 的完整安装路径命令。具体命令见[项目首页的手动应急更新](../README.md#手动应急更新-cookie)。不要直接编辑 `rsshub.env`，否则会绕过格式检查、健康检查和回滚事务。

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

## X/Twitter

先确认服务端、Native Host、扩展都升级到了包含 Twitter 支持的版本，并在重新加载扩展后点击“授权站点权限”。只更新扩展时，旧 Native Host 或服务端会拒绝 `twitter` provider。

- `twitter_not_configured`：服务端没有 X live 令牌，正常等待首次有效同步，不发送 X 登录失效提醒。
- `twitter_token_pool_unsupported`：已有 `TWITTER_AUTH_TOKEN` 是多账号列表，自动接管已停止，原值仍保留。当前版本只自动管理单账号；需要保留令牌池时继续手动管理。
- `twitter_csrf_missing`：X 首页没有返回唯一可用的临时 `ct0`，本次验证暂不可用。
- `twitter_invalid_response`、`http_404`：账户接口可能变化或返回风控页面，不表示令牌已失效。
- `http_403`、`http_429`、超时或 `5xx`：按临时上游故障重试；持续失败沿用现有 Bark 故障提醒，不覆盖 live 或有效候选。
- `http_401`、`twitter_auth_failed`：明确认证失败。定时监控连续两次失败后才尝试有效候选；没有有效候选时提醒重新登录。

服务端必须直接访问 `x.com` 和 `api.x.com`，不会继承 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`。不要通过把凭证发到自定义验证地址来排查。模拟测试只验证本项目的响应处理和回滚行为，部署后仍需用“立即同步”和脱敏服务端状态确认真实链路。

扩展读取失败也可能是同一域出现多个不同的 `auth_token`。请在同一个 Edge Default Profile 重新登录 X；不会静默选取冲突值或混合两个域的 Cookie。X/Twitter 的“复制 Auth Token”输出裸令牌，可通过 `manual-update --provider twitter` 的隐藏提示输入。

## 扩展诊断日志与单站点重试

此功能自 v1.2.2 起提供，需更新扩展、Native Host 和服务端。旧 v1.2.0 安装不会自动获得这些日志能力；旧请求仍保持原有 v1 行为，新请求使用可选的 `diagnostics: true` 获取有限错误码。

在弹窗展开“诊断日志”，点击失败站点卡片的“重试此站点”，再刷新日志。它只采集和上传该站点，不会重试其他站点。每次记录采集开始、上传及服务端处理开始、最终结果与总耗时；卡片时间表示最近尝试时间，不保证成功。

- 停在采集阶段：查看权限、浏览器读取或凭证格式错误。
- `host_configuration_invalid`：本机 Host 配置或运行文件检查失败。
- `ssh_auth_failed`：SSH 明确返回公钥认证失败，此时才检查对应 `.pub` 是否授权。
- `ssh_host_key_failed`：主机指纹检查失败，核对服务器身份，不要关闭检查。
- `ssh_connection_failed` / `ssh_timeout`：连接或远程执行失败、超时，不等同于公钥错误。
- `http_403` / `http_429` / `network_error`：服务端上游探针返回错误，与 SSH 认证不同。
- `twitter_csrf_missing`：X 首页未提供可用的 CSRF Cookie。
- `server_error`：远程程序非正常退出，可能涉及旧版本不支持诊断请求、部署配置或更新事务，需检查服务端状态。

日志最多保留 200 条，可导出为 JSON；清空仅删除历史日志，不影响配置与 Cookie。正在同步时清空后仍可能出现新完成记录。日志刷新、导出、清空不会读取 Cookie 或启动 SSH。日志写入失败不阻断同步。日志不包含原始 Cookie、令牌、指纹、服务器地址、HTTP 响应体、SSH stderr 或密钥文件内容。
