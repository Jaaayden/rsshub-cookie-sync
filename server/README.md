# RSSHub Cookie Sync 服务端

[返回项目总览](../README.md)

这一目录是运行在 RSSHub 服务器上的服务端组件。它使用 Python 3.9+ 标准库，不提供公网 HTTP API，也不会主动连接客户端电脑。

服务端负责四件事：接收 Edge 上传的候选 Cookie、从服务器网络验证登录态、在连续认证失败后安全切换候选、以及通过 systemd timer 监控 RSSHub 和发送 Bark 告警。

## 运行要求

- Linux 服务器，root 权限，systemd；
- Python 3.9 或更高版本；
- Docker Compose v2，且支持 `env_file` 的 `format: raw`；
- RSSHub 已经能由 Docker Compose 启动，并且目标服务有本机可访问的 `/healthz`；
- 服务器本身可以访问 `www.zhihu.com` 和 `m.weibo.cn`。客户端的代理不会被转发到服务器；
- SSH 服务端支持 Ed25519 主机密钥。

默认配置只允许把 RSSHub 健康检查指向 loopback 地址（例如 `http://127.0.0.1:1200`），避免 Cookie 同步器被配置成 SSRF 代理。知乎、微博探针 URL 也固定为源码中列出的官方地址。

## 安装前准备

先确认 Compose 文件的绝对路径、Compose project 名和 RSSHub service 名。默认安装器使用 project `rsshub`、service `rsshub`；如果你的 RSSHub 使用了不同名称，传入对应选项。源码必须放在 root-owned、且 group/world 不可写的目录（例如由 root 克隆到 `/opt/rsshub-cookie-sync`），安装器会拒绝从可被普通用户替换的目录安装 root 程序。

安装器只应对你明确指定的 Compose 文件执行。示例（路径和名称均为占位符）：

```sh
cd /path/to/rsshub-cookie-sync/server
./install.sh --compose-file /absolute/path/to/docker-compose.yml
```

完整示例为：

```sh
./install.sh \
  --compose-file /absolute/path/to/docker-compose.yml \
  --project-name <compose-project> \
  --service-name <rsshub-service> \
  --rsshub-base-url http://127.0.0.1:1200
```

请先运行 `./install.sh --help`，以当前版本输出为准。安装器会拒绝相对路径、带空格或特殊字符的路径、符号链接、不属于 root 或可被 group/world 写入的源码与目标路径；它也会先校验 Compose，校验失败时不会迁移密钥。

## 安装器会修改什么

这是一个有意的、范围很小的迁移。针对指定 service，安装器会：

1. 从该 service 的 `environment` 中取出 `ZHIHU_COOKIES`、`WEIBO_COOKIES`、`TWITTER_AUTH_TOKEN`；
2. 从 Compose 文本中删除这三项的明文值；
3. 在该 service 增加以下 Compose 相对路径引用：

   ```yaml
   env_file:
     - path: ./secrets/rsshub.env
       format: raw
   ```

4. 把三项写入 Compose 文件旁的 `secrets/rsshub.env`，并设置目录 `0700`、文件 `0600`；
5. 使用 `docker compose config --quiet` 校验，再只重建目标 RSSHub service：

   ```sh
   docker compose -p <compose-project> \
     -f /absolute/path/to/docker-compose.yml \
     up -d --no-deps --force-recreate --pull never <rsshub-service>
   ```

其他 service、镜像版本、网络、卷、端口和普通环境变量不会被迁移逻辑主动改写。`format: raw` 用来避免 Cookie 中的 `$`、`#` 等字符被 Compose 插值。

迁移和重建期间会在 Compose 文件旁及 secrets 目录中使用 root-only 临时事务文件。成功后这些临时备份会删除，避免历史 Cookie 长期残留；迁移、Compose 校验、健康检查或 provider 复检失败时会恢复事务备份，并且不会启用监控 timer。

## 首次安装与全新 RSSHub

有两种合法的初始状态：

- 已有知乎和微博 Cookie：安装器会迁移并验证两者，验证成功后启用 timer；
- 全新 RSSHub 尚无这两项 Cookie：安装器会创建空的 raw secret env，只检查 Compose 和 RSSHub `/healthz`，将状态标记为 `unseeded`，然后启用 timer。第一次从 Edge 收到某个 provider 的有效 Cookie 后，会立即保存并在需要时提升为 live 配置。

只有一项 Cookie 存在而另一项为空时，安装器会拒绝继续，避免把不完整配置误当作全新实例。请补齐两项，或者清空两项后再执行全新初始化。

首次安装会重建一次目标 RSSHub service，但不会执行 Docker pull、不会重建依赖、不会 `down`，也不会清空 Redis。安装同一实例的后续版本时，安装器应识别已经完成的迁移并跳过重复重建；不要为了“刷新安装”手动把密钥写回 Compose。

## 文件布局与权限

安装完成后，常见文件如下（具体路径由安装器固定管理）：

| 路径 | 用途 | 权限/边界 |
| --- | --- | --- |
| `/usr/local/lib/rsshub-cookie-sync/` | 服务端 Python 程序 | root 管理 |
| `/usr/local/sbin/rsshub-cookie-sync-apply` | SSH forced command 的唯一入口 | root-owned，可执行 |
| `/etc/rsshub-cookie-sync/config.json` | RSSHub 地址、探针和可选 Bark 配置 | `0600`，root-owned |
| `<compose-dir>/secrets/rsshub.env` | 当前 live Cookie 和 `TWITTER_AUTH_TOKEN` | `0600`，root-owned |
| `<compose-dir>/secrets/candidates/` | 候选 Cookie 文件 | `0700`，root-owned |
| `/var/lib/rsshub-cookie-sync/state.json` | 脱敏状态与计数 | root-owned |
| `/var/lib/rsshub-cookie-sync/lock` | apply、monitor、迁移共用的持久锁 | root-owned |

`config.json` 不含 Cookie，但启用 Bark 后会保存 Device Key，因此必须保持 `0600`。状态文件和日志不会包含 Cookie 原文、Bark Device Key、完整请求头或完整上游响应；`status --json` 只返回固定字段和脱敏状态。

安装器还会创建禁用密码登录的 `rsshub-sync` 系统账号。它没有 Docker 组权限，只能使用受限的 sudo wrapper；SSH key 通过 forced command、禁止 PTY、转发和 Agent 转发进一步限制。

## Bark 配置

Bark 是可选的。Device Key 不要放进 URL、shell 参数、Git 或日志。安装后以 root 运行 `configure-bark`，程序会从 stdin 读取一行并立即退出：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json configure-bark
```

命令会等待一行输入。粘贴完整 Bark URL 或单独的 Device Key，交互输入不会回显，按回车即可。不要把真实 key 写进命令历史；不要使用带 key 的 URL 作为命令参数。程序只接受 Bark V2 的 `https://api.day.app`，实际请求使用 `/push` 的 JSON body，不把 Device Key 放在请求 URL 中。

发送一次测试通知：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json notify-test
```

没有配置 Bark 也不影响 Cookie 验证、候选保存和自动切换，只是不发送通知。

## 查看状态与手动操作

查看脱敏 JSON 状态：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json status --json
```

状态包含两个 provider 的探针结果、认证失败和临时失败计数、候选接收/验证时间、最近成功时间、RSSHub 健康状态和是否存在待处理事务；不会包含 Cookie 或哈希原文。

systemd 调度：

```sh
systemctl status rsshub-cookie-sync-monitor.timer
systemctl list-timers rsshub-cookie-sync-monitor.timer
journalctl -u rsshub-cookie-sync-monitor.service -n 100 --no-pager
```

timer 每 15 分钟检查一次；首次安装后通常约 5 分钟触发。连续两次明确的 `401`/登录失效才会尝试切换有效候选。`403`、`429`、`432`、超时和 `5xx` 属于临时上游故障，不会替换 Cookie；连续多次后才告警。

## SSH 公钥接入

Native Host 安装程序会在 Mac 上生成专用 Ed25519 密钥。完成 Native Host 安装后，在 **Mac 终端**运行下面的命令，把公钥通过 SSH stdin 交给服务器：

```sh
ssh -p <服务器 SSH 端口> root@<服务器地址> \
  /usr/local/sbin/rsshub-cookie-sync-provision-key \
  < "$HOME/Library/Application Support/RSSHub Cookie Sync/ssh/id_ed25519.pub"
```

这条命令的参数中不含公钥内容；公钥只通过 stdin 传输。服务器端会原子替换 `rsshub-sync` 的受限 `authorized_keys`。首版只管理一个采集端；再次 provision 新公钥会撤销旧采集端。不要手工改成普通 shell key，也不要把私钥上传到服务器或提交到 Git。

Native Host 的安装、代理选项、`known_hosts` 带外校验和 Edge 连接步骤见 [Native Messaging Host 文档](../native-host/README.md)。

## 重装、升级和迁移

### 在同一台服务器上重装

使用与首次安装完全相同的 Compose 路径、project 和 service 参数再次运行安装器。已迁移的 Compose 会保留 `env_file` 结构和当前 secret env，Bark 配置、候选、状态、SSH 公钥和 timer 也会保留。正常重装不会把 Cookie 写回 Compose，也不应无条件重建 RSSHub。

如果需要更换 Compose 路径、project 或 service，请把它视为一次新的部署迁移：先完整备份目标 Compose 和 secrets，使用新参数明确执行；不要让普通升级命令静默改指向另一套 RSSHub。

### 升级程序

更新 root-owned 源码后，使用与首次安装完全相同的全部参数运行安装器。安装器会自动暂时停用 timer，并在成功后恢复；不要在外部先停用，否则安装器无法知道它原本应为启用状态：

```sh
./install.sh \
  --compose-file /absolute/path/to/docker-compose.yml \
  --project-name <compose-project> \
  --service-name <rsshub-service> \
  --rsshub-base-url http://127.0.0.1:1200
```

具体升级动作以发布版本说明为准。若升级失败，先查看 `journalctl` 和 `status --json`，不要删除 `rsshub.env`。

## 卸载

在项目的 `server` 目录运行：

```sh
./uninstall.sh
```

卸载只停用并删除同步器的 systemd 单元、受限 SSH 账号/公钥、sudoers、Native Host apply 入口和同步器配置；它不会把三个 secret 写回 Compose，不会删除当前 `rsshub.env`、RSSHub Compose、RSSHub 容器、候选/状态数据。这样卸载后 RSSHub 仍可按最后一次 live 配置运行。若要彻底清理状态和 secrets，请在确认不再需要回滚后由管理员逐个检查并手动删除，切勿使用宽泛的递归删除命令。

卸载后重新安装会复用保留下来的 live env、候选和状态，但需要重新配置 Bark，并把 Native Host 公钥重新 provision 到新建的受限账号。普通版本升级无需先卸载。

## 故障排查

1. `config --quiet` 失败：先单独运行 `docker compose -f <compose-file> config --quiet`，确认 Compose 版本支持 `format: raw`，并检查 YAML 的 service 名。
2. RSSHub 健康检查失败：检查目标 service 的容器状态和本机健康地址；不要把公网地址填入 `rsshub.base_url`。
3. provider 探针认证失败：Edge 重新登录并点击“立即同步”；服务端会先保存候选，验证成功后立即修复失效的 live Cookie。
4. `retryable_error`：检查服务器到知乎/微博的 DNS、出口、防火墙和临时限流；临时故障不会触发 Cookie 替换。
5. SSH 上传失败：检查 `rsshub-sync` 公钥是否由 `provision-key` 写入、客户端 `known_hosts` 是否与服务器指纹一致、Native Host 配置是否有正确的服务器地址。
6. 没有通知：检查是否配置 Bark、`notify-test` 是否成功，并查看同步器日志中的状态码和分类原因；日志不会显示 key 或 Cookie。

## 本地测试

在仓库根目录运行：

```sh
make test
make check
```

服务端测试会使用模拟的 HTTP、Docker 和文件系统边界，覆盖输入限制、候选验证、Compose 迁移、并发锁、容器不健康、回滚和崩溃恢复；不会连接你的服务器，也不会读取当前用户的真实 Cookie。
