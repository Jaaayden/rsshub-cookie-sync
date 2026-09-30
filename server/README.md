# RSSHub Cookie Sync 服务端

[返回项目总览](../README.md)

这里的程序运行在 RSSHub 所在的 Linux 服务器上。默认接收 Edge 上传的凭证并直接安全更新 RSSHub，不调用独立上游登录探针。systemd 每 15 分钟检查服务及配置的实际订阅路由，详见 [同步模式与订阅监控](../docs/route-monitoring.md)。

服务端不提供公网 API，不会主动连接你的 Mac，也不需要安装 Python 第三方包。

## 运行要求

- Linux + root 权限 + systemd；
- Docker Engine 和 Docker Compose v2.30 或更高版本；
- `sudo` 软件包（安装器用 `visudo` 验证受限账号的最小权限）；
- 已经可以启动的 RSSHub Compose 文件；
- 服务器能直接通过 HTTPS 访问知乎、微博及 `x.com`、`api.x.com`；
- SSH 服务正在运行。主机密钥类型由 OpenSSH 协商，不限定为 Ed25519。

`format: raw` 是为了原样读取 Cookie 中的特殊字符，需要 Compose v2.30+。版本不足时先升级 Compose，不要把 Cookie 改回 YAML。

## 一键安装

新手推荐先在 Mac 上运行项目首页的本机安装命令，复制安装器显示的 `ssh-ed25519` 公钥；然后从 Mac 普通 SSH 登录服务器一次，在首次主机指纹提示处确认并输入 `yes`。保持这个 root shell 不要退出，直接在同一个会话中执行：

```sh
ssh -p <服务器SSH端口> root@<服务器地址>
```

进入服务器后执行：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh
```

一键脚本下载最新稳定版本，并启动本目录的交互式安装器。它会：

1. 在当前目录、`/opt/rsshub`、`/root/rsshub` 等常见位置查找 Compose 文件，让你确认后再操作；
2. 默认选择官方文件常见的 `rsshub` service；如果没有该名称，会列出实际服务并让你选择；
3. 让 Docker Compose 自动解析 project 名称；
4. 默认把 RSSHub 健康地址设为 `http://127.0.0.1:1200`；
5. 从选中的 RSSHub service 经 Docker Compose 展开后的环境配置中读取 `ACCESS_KEY`，保存到同步器配置供健康检查使用；不会把密钥打印出来；
6. 询问是否配置 Bark；
7. 在开始部署前要求粘贴 Native Host 公钥；把刚才在 Mac 上复制的那一整行公钥粘贴进去。

普通安装不需要填写 project、service 或健康地址，也不需要另开终端执行 `provision-key`。Compose 在其他位置时，安装器会提示输入绝对路径。项目源码也可以直接安装：

```sh
cd /path/to/rsshub-cookie-sync/server
sudo ./install.sh
```

这里的 `/path/to/...` 只是示例路径，不能照抄。出于防篡改要求，源码目录、Compose 文件及其父目录必须由 root 拥有，且不能让 group/world 写入；因此不要从普通用户可写的 home 目录直接执行源码安装。新手直接使用上面的一键命令即可。安装器不会向服务器拉取 RSSHub 镜像，不会连接客户端，也不会执行 `docker compose down`。

首次读取的值来自所选 service 的有效 Compose 配置：包含该 service 的 `environment` 与 `env_file`，并遵循 Compose 对 `.env` 插值和环境优先级的解析结果。配置里没有 `ACCESS_KEY` 时，健康检查沿用无 key 模式；如果之后增加或更换了 `ACCESS_KEY`，请在目标 RSSHub 已按新 Compose 配置启动后运行[刷新命令](#刷新-rsshub-连接配置)。

## 安装器会修改什么

安装器只处理被选中的 RSSHub service，并只迁移这三项：

```text
ZHIHU_COOKIES
WEIBO_COOKIES
TWITTER_AUTH_TOKEN
```

如果它们原来写在 service 的 `environment` 中，安装器会：

1. 把值保存到 Compose 文件同目录的 `secrets/rsshub.env`；
2. 从 YAML 中移除这三项明文值；
3. 在该 service 中加入：

   ```yaml
   env_file:
     - path: ./secrets/rsshub.env
       format: raw
   ```

`format: raw` 防止 `$`、`#` 等字符被 Compose 插值。`secrets` 目录为 `0700`，env 文件为 `0600`，Compose 文件也会收紧为 `0600`。

其他 service、端口、网络、卷、镜像版本和普通环境变量不会被迁移逻辑主动改写。更新 Cookie 时只重建目标 RSSHub service：不会重建 Redis 或 browserless，不会拉取镜像，不会删除 volume，也不会清空 Redis。因为 RSSHub 进程只在启动时读取环境变量，所以更新 live Cookie 必须重建该 service。

安装器在写入期间会创建 root-only 的事务备份。Compose 校验、容器健康检查或 provider 复检失败时会恢复旧 env 并尝试重建；成功后临时备份会删除，不会积累历史 Cookie。

如果是全新 RSSHub，原 Compose 没有这三项，安装器会创建空的 raw env 文件并接入目标 service。之后第一次收到有效 Cookie 时才会写入；一方失败不会清空另一方。

因此答案很明确：首次接入会修改原 `docker-compose.yml`，但范围仅限于把三项 secret 迁到 `env_file`；日常同步不会把 Cookie 写回 YAML。

## X/Twitter

`twitter` 同步无需新增配置；订阅监控需配置 `rsshub_route`。仅在显式选择 `sync_mode: "verified"` 时，探针固定访问 `https://x.com/` 获取临时 `ct0`，再携带 Web 客户端认证头访问 `https://api.x.com/1.1/account/settings.json`。只有包含有效账户名的成功 JSON 才表示登录有效；不跟随重定向，不持久化临时 CSRF Cookie，不读取环境代理。

请求使用协议 v1 的 `cookieHeader: "auth_token=值"`；live env 的 `TWITTER_AUTH_TOKEN`、候选文件和服务端哈希均使用裸令牌。手动隐藏输入同时接受裸令牌与单个 `auth_token=值`。

未配置 X 时不探测、不告警；旧状态自动补齐 Twitter 字段。初始化只检查已配置的 provider，`bootstrap.status=seeded` 表示至少配置了一项，不代表所有站点健康。已有多账号令牌池会保留并报告 `twitter_token_pool_unsupported`，不会用浏览器单账号覆盖。

先升级服务端，再升级 Mac Native Host 和 Edge 扩展；保留已有配置、密钥和 live env。暂时失败不会删除候选或切换 live。真实接口可用性取决于部署网络和 X 当前接口，参阅 [故障排查](../docs/troubleshooting.md#xtwitter)。

## Bark 配置

Bark 是可选的。不配置时同步、自愈和服务器监控仍然工作，只是不推送通知。

安装器会询问是否现在配置。之后也可以在服务器上执行：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json configure-bark
```

程序会隐藏交互输入；可以粘贴完整的 `https://api.day.app/<DeviceKey>` 或单独的 Device Key。不要把 key 写入命令参数、环境变量或脚本。测试通知：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json notify-test
```

Device Key 保存在服务器权限为 `0600` 的配置文件中，请勿把命令输出或配置文件发给别人。

## Native Host 公钥

Native Host 安装器会在 Mac 上默认创建 `~/.ssh/rsshub-cookie-sync`，并在屏幕上显示对应的公钥。新手流程中不需要单独执行授权命令：保持首次普通 SSH 登录进入的 root shell，在服务端安装器要求粘贴 Native Host 公钥时，直接粘贴这一整行公钥即可。

公钥通过安装器的标准输入传入服务器，不会出现在远程命令参数中；私钥永远不上传。服务器安装器会创建固定的 `rsshub-sync` 受限账号并安装这把公钥。首次安装时如果跳过了公钥，或需要更换密钥，请阅读[高级 SSH 说明](../docs/advanced-ssh.md)中的单独 provision 和迁移流程；不要把它作为新手安装步骤。

服务器为同步器创建独立的 `rsshub-sync` 账号：禁用密码登录、无 Docker 组权限，只能运行固定的受限命令。不要把这个账号改成普通 shell 账号，也不要手工追加任意命令。

## 手动应急更新 Cookie

扩展的知乎、微博卡片各有一个“复制 Cookie”按钮，X/Twitter 卡片提供“复制 Auth Token”。需要手工处理时，复制对应 Cookie，以 root 登录服务器并运行：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync manual-update --provider zhihu
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync manual-update --provider weibo
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync manual-update --provider twitter
```

在提示后粘贴并按回车，内容不会回显。命令会使用与自动上传完全相同的输入限制、同步模式和事务回滚；它不会把 Cookie 放进命令参数或日志，也不要求手工编辑 Compose。格式错误会被拒绝；默认模式不判断上游凭证有效性。

## 文件和权限

安装后主要文件如下：

| 路径 | 用途 |
| --- | --- |
| `/usr/local/lib/rsshub-cookie-sync/` | 服务端程序 |
| `/usr/local/sbin/rsshub-cookie-sync-apply` | SSH forced command 唯一入口 |
| `/etc/rsshub-cookie-sync/config.json` | 部署地址、健康检查和可选 Bark 配置，`0600` |
| `<Compose目录>/secrets/rsshub.env` | live Cookie 和 `TWITTER_AUTH_TOKEN`，`0600` |
| `<Compose目录>/secrets/candidates/` | 已验证但待命的候选 Cookie，`0700` |
| `/var/lib/rsshub-cookie-sync/state.json` | 脱敏状态和失败计数 |
| `/var/lib/rsshub-cookie-sync/lock` | 更新与监控共用的锁 |

日志、状态和 `status --json` 不包含 Cookie、Bark Device Key、完整请求头或上游响应。

## 查看状态

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json status --json
```

查看定时器和最近日志：

```sh
systemctl status rsshub-cookie-sync-monitor.timer
systemctl list-timers rsshub-cookie-sync-monitor.timer
journalctl -u rsshub-cookie-sync-monitor.service -n 100 --no-pager
```

默认每 15 分钟检查 RSSHub 服务及配置的订阅路径，连续两次路由异常通知，恢复通知；不会因此更换凭证。仅 `verified` 模式使用旧上游探针：连续两次明确认证失败才会尝试切换已验证候选；`403`、`429`、`432`、超时和 `5xx` 会暂时归类为上游故障，不会立即换 Cookie。

## 刷新 RSSHub 连接配置

当 RSSHub Compose 中的 `ACCESS_KEY` 或本地访问地址发生变化时，在服务器 root shell 中运行。可以先预览，不保存配置：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync refresh-rsshub-config --dry-run
```

预览显示健康检查通过后，再保存新配置：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync refresh-rsshub-config
```

命令会从已配置的 Compose 文件和 service 重新读取 `ACCESS_KEY`，使用当前健康地址检查运行中的 RSSHub；只有健康检查成功才会原子更新同步器配置，旧配置保持原样直到验证通过。密钥不会出现在命令参数、输出或日志中。`--dry-run` 会取得部署锁，并进行只读的 Docker/HTTP 查询；首次执行可能创建锁文件，但不会写配置、重建 RSSHub 或恢复待处理事务。

如果本机 RSSHub 地址或端口也改了，先让新地址上的 RSSHub 按 Compose 配置运行，再将新地址传给刷新命令。例如：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync refresh-rsshub-config \
  --rsshub-base-url http://127.0.0.1:1300
```

如果只改了 Compose 内的 `ACCESS_KEY`，先按平常方式应用 Compose 变更并确认 RSSHub 已使用新 key 启动，然后执行刷新。否则健康检查仍会遇到旧运行配置，命令会拒绝保存新值。刷新不会重建 RSSHub，也不会删除事务文件；下一次正常同步或监控会按现有事务流程处理待恢复状态。可选地在操作前以 root-only 权限备份 `/etc/rsshub-cookie-sync/config.json`；不要直接编辑或展示其中的密钥。

## 升级和重装

同一实例升级或重装时，直接再次执行同一条一键命令即可。安装器会识别已迁移的部署，自动复用原来的 project、service 和非默认健康端口，并保留：

- 当前 live Cookie 和候选；
- Bark 配置；
- 状态计数；
- `rsshub-sync` 账号和公钥；
- 已有 Compose 的 `env_file` 结构。

它不会把 secret 写回 Compose，也不会因为重装而无条件重建 RSSHub。不要先删除 `secrets/rsshub.env`，也不要先运行卸载脚本。

只有在明确要切换到另一套 Compose 或另一套 RSSHub 时，才使用 [Compose 高级说明](../docs/advanced-compose.md) 中的兼容参数，并先确认目标文件。普通安装不要学习或填写这些参数。

## 卸载

安装时已经提供固定的卸载入口，直接执行：

```sh
sudo /usr/local/sbin/rsshub-cookie-sync-uninstall
```

如果当前 root shell 中没有这个入口，也可以使用服务端 curl bootstrap：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh -s -- uninstall
```

Release 也会附带同版本 `uninstall-server.sh` 和 `SHA256SUMS`，便于固定版本应急卸载。正常情况优先使用安装后的 `/usr/local/sbin/rsshub-cookie-sync-uninstall`，它与当前安装的服务端程序必然同版本。

不要在 RSSHub 仍需要运行时删除 Compose 或 secret 文件。

卸载会停用同步器 timer，移除同步器程序、systemd 单元和配置，同时删除候选 Cookie 和 `/var/lib/rsshub-cookie-sync` 状态；只删除确认由本项目创建的 `rsshub-sync` 账号。如果安装前已经存在同名账号，卸载只移除本项目添加的 SSH 授权和相关配置，不删除账号本身。不会把 secret 写回 Compose，也不会删除当前 `rsshub.env`、RSSHub 容器、volume 或 Redis。这样 RSSHub 会继续使用最后一次 live 配置运行。确认以后不再需要 RSSHub 使用这些 secret 后，再由管理员手工清理 `rsshub.env`。

完整卸载请遵循“服务端 → Edge → Mac”：先完成本节服务端卸载，再在 `edge://extensions` 移除扩展，最后在 Mac 使用安装后的固定卸载入口或 macOS bootstrap。服务端删除候选 Cookie 和 `/var/lib/rsshub-cookie-sync` 状态，只保留 Compose、运行中的 RSSHub 和 live `rsshub.env`；Mac 默认保留 `~/.ssh/rsshub-cookie-sync` 及其 `.pub`。

## 常见问题

### 安装器找不到 Compose

确认文件名是 `docker-compose.yml`、`docker-compose.yaml`、`compose.yml` 或 `compose.yaml`，然后重新运行安装器，在提示处输入绝对路径。路径应由 root 拥有，且不能让普通用户写入。

### 安装器说没有 `rsshub` service

这表示 Compose 文件中的 service 名称不同。交互式安装器会列出实际名称，选择真正运行 RSSHub 的那一项即可。只有多实例或非标准布局才需要查看 [Compose 高级说明](../docs/advanced-compose.md)。

### Cookie 被拒绝

`rejected_invalid` 表示对应 provider 的登录态探针未通过。先在 Edge 重新登录该网站，然后在扩展中点击“立即同步”。无效值不会覆盖 live Cookie，也不会触发 RSSHub 重建。

### 没有 Bark 通知

确认已运行 `configure-bark`，再运行 `notify-test`。通知失败不会影响 Cookie 更新。

### SSH 无法连接

检查 Mac 扩展“连接设置”中的服务器地址、端口、密钥文件名，检查服务器上的公钥是否对应所选密钥，并确认 `~/.ssh/known_hosts` 中与该地址和端口匹配的主机指纹已通过可信渠道核对。普通或哈希记录、非 22 端口，以及 OpenSSH 支持的 RSA、ECDSA 或 Ed25519 主机密钥都可以使用。不要关闭主机密钥校验。

## 相关说明

- [项目总览和新手安装](../README.md)
- [Native Host 安装](../native-host/README.md)
- [Compose 高级说明](../docs/advanced-compose.md)
- [安全策略](../SECURITY.md)
