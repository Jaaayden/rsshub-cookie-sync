# RSSHub Cookie Sync

[![CI](https://github.com/Jaaayden/rsshub-cookie-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/Jaaayden/rsshub-cookie-sync/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Jaaayden/rsshub-cookie-sync)](https://github.com/Jaaayden/rsshub-cookie-sync/releases)

RSSHub Cookie Sync 是一个独立的开源工具，把 Microsoft Edge Default Profile 中已经登录的知乎、微博 Cookie 安全地同步到 RSSHub，并在登录态失效时告警、切换候选 Cookie。它不修改 RSSHub 源码，不依赖 CookieCloud，也不要求手动复制 Cookie。

```text
Edge 登录态
    │  扩展只读取两个目标请求会发送的 Cookie
    ▼
Edge MV3 扩展 ── Native Messaging ── 本机 Python Host
                                      │ Cookie 只走 stdin
                                      ▼
                               受限 SSH / forced command
                                      ▼
                              RSSHub 服务器 Python wrapper
                                      │ 预检、候选、事务切换
                                      ▼
                 root-only env_file ── RSSHub 容器
                                      │
                          systemd timer + Bark（可选）
```

> Cookie 是登录凭证。不要把真实 Cookie、Bark Device Key、SSH 私钥、真实服务器地址或带 secret 的 Compose 输出提交到 GitHub、Issue、日志、剪贴板或聊天记录。

## 1. 工作方式和边界

- Edge 启动时、每 15 分钟以及目标 Cookie 变化后尝试同步；Cookie 变化会合并等待约 2 分钟。
- 服务端 systemd timer 每 15 分钟检查 RSSHub 健康状态和两个 provider 的登录态。
- 连续两次明确认证失败后，只切换已经预检通过且不同于当前 live 的候选 Cookie。
- 超时、`429`、`432`、`5xx` 等临时上游故障不会被误判为 Cookie 失效；持续异常才告警。
- 切换使用 `flock`、原子 env 写入、Compose 校验、容器健康检查和 provider 复检；失败会回滚。
- Bark 只发送自动切换、需要重新登录、持续上游异常、回滚和恢复等事件，不发送周期性成功消息。

Cookie 原文只会在扩展读取、Native Host 内存、SSH stdin、服务端预检及 root-only env/candidate 文件中存在。RSSHub 仍会把环境变量放入容器进程环境，因此拥有服务器 root 或 Docker 管理权限的人仍可能读取它；这是宿主机信任边界。

本项目不会：

- 保存或输入知乎/微博密码，不绕过验证码或 MFA；
- 因 RSS 长时间没有新文章就判断登录失效；
- 新增公网接收 API、使用 CookieCloud、修改 RSSHub 源码；
- 执行 `docker compose down`、Docker image pull、volume 删除或 Redis 清空。

## 2. 支持范围

- 客户端：macOS + Microsoft Edge Default Profile。
- 服务端：Linux + systemd + Docker Compose v2.30 或更高版本。
- Python 3.9+ 标准库即可，无第三方 Python 依赖。
- SSH 目标支持 hostname 或 IPv4，使用已带外核对的 Ed25519 主机密钥。
- 客户端到服务器默认直连，也可显式指定 SOCKS5；代理失败不会自动回退直连。
- Bark V2 可选。Edge 关闭或 Mac 睡眠时不能采集新 Cookie，但服务器监控仍可工作。

`env_file.format: raw` 需要 Compose 2.30+。先执行 `docker compose version`，版本不足时先升级 Compose，不要把 Cookie 拼回 YAML。

## 3. 配置项与公开固定标识

这些值由每个部署者自行配置，不写死在程序中：

| 项目 | 配置方式 |
| --- | --- |
| Compose 路径 | `server/install.sh --compose-file`（必填） |
| Compose project/service | `--project-name`、`--service-name` |
| RSSHub 健康地址 | `--rsshub-base-url` |
| 服务器 host/port/user | `native-host/install.py` 的 `--server-host`（必填）、`--server-port`、`--server-user` |
| 本机 SOCKS5 | 同时指定 `--proxy-host` 和 `--proxy-port` |
| Bark | 服务端配置；Device Key 通过 stdin 输入 |

以下是公开的产品标识，不是凭证：Native Host 名称 `com.jayden.rsshub_cookie_sync`、用于固定扩展 ID 的公开 manifest key、扩展 ID 和专用系统账号名 `rsshub-sync`。它们固定不会泄露任何部署的服务器地址、Cookie、Bark Key 或私钥。

固定 manifest key 只保证扩展 ID 稳定，**不证明扩展代码来自本仓库，也不是数字签名**。加载解压缩扩展前，应从本仓库或 GitHub Release 获取文件并核对来源，不要加载来历不明、复用了同一公开 key 的目录。Native Host 的 `allowed_origins` 只能限制扩展 ID；它不能替代源码/Release 完整性检查。本机同一用户权限下的恶意程序本来就可能读取专用 SSH key，因此还应保护扩展目录和 Mac 用户账号。

## 4. 准备

### 4.1 Mac

```sh
python3 --version
ssh -V
node --version
```

Node.js 只用于测试，正式运行不依赖 npm。用 Edge Default Profile 登录：

- `https://www.zhihu.com`
- `https://m.weibo.cn`

微博要保持移动站点登录态；本项目不会代替用户登录。

### 4.2 服务器

服务器需要 root 或等价本地管理权限、Docker Engine、Compose v2.30+、systemd，并能从服务器自身访问知乎、微博和（如启用）Bark。目标 Compose 文件以及源码目录必须是 root-owned、不可由 group/world 写入的普通路径；为了让 systemd 路径保持无歧义，首版安装器不接受含空格或特殊字符的 Compose 路径。

确认正确的 Compose project 和 service。安装器会只重建一个目标 service，不能凭目录名猜：

```sh
docker compose version
docker ps --format '{{.Names}}\t{{index .Labels "com.docker.compose.project"}}\t{{index .Labels "com.docker.compose.service"}}'
docker compose -f /opt/rsshub/docker-compose.yml config --services
```

记下确实对应 RSSHub 容器的 project/service。常见值是 `rsshub` / `rsshub`，但必须以实际标签和 Compose 配置为准；顶层有 `name:` 时也不要猜。

## 5. 服务端首次安装

下面都使用示例路径 `/opt/rsshub/docker-compose.yml` 和示例域名 `rsshub.example.com`。替换为自己的值即可，但不要把真实值写回仓库。

### 5.1 获取源码

在服务器上以 root 执行：

```sh
install -d -m 0755 /opt
git clone https://github.com/Jaaayden/rsshub-cookie-sync.git /opt/rsshub-cookie-sync
cd /opt/rsshub-cookie-sync
```

不要把包含 secret 的 RSSHub 目录作为 Git 工作目录。

### 5.2 运行安装器

假设 Compose 文件、project、service 和健康地址如下：

```sh
cd /opt/rsshub-cookie-sync
chmod 0755 server/install.sh
server/install.sh \
  --compose-file /opt/rsshub/docker-compose.yml \
  --project-name rsshub \
  --service-name rsshub \
  --rsshub-base-url http://127.0.0.1:1200
```

`--compose-file` 必填；其余三个参数按实际部署填写，建议每次重装都显式写出。健康地址应是服务器本机能访问的 RSSHub 地址，例如回环地址，不要填不受控的公网地址。

安装器会校验目标 Compose/service、权限、Compose 版本和 systemd，写入不含 Cookie 的 `/etc/rsshub-cookie-sync/config.json`，创建权限受限的 `rsshub-sync` 账号、sudoers、sshd Match、forced command 和 systemd timer，并把程序装到 `/usr/local/lib/rsshub-cookie-sync`。

安装器不会连接客户端，不会 Docker pull。若校验、健康检查或回滚检查失败，应先看错误，不要手动删除 transaction 文件。

## 6. Compose 和 secret 的变化

安装器只处理 `--compose-file` 中 `--service-name` 指定的 service，只迁移这三项：

```text
ZHIHU_COOKIES
WEIBO_COOKIES
TWITTER_AUTH_TOKEN
```

### 6.1 已有 Compose inline Cookie

安装器会从目标 service 的 `environment` mapping 或 `KEY=value` list 提取上述三项，把它们放到 Compose 文件同目录的 `secrets/rsshub.env`，然后从 YAML 移除 inline secret 并加入：

```yaml
env_file:
  - path: ./secrets/rsshub.env
    format: raw
```

`rsshub.env` 权限为 `0600`，`secrets` 目录为 `0700`，Compose 文件为 `0600`。其他 environment、端口、卷、网络、健康检查和其他容器配置会保留。

迁移完成后会先使用 `docker compose ... config --quiet`，再执行目标 service 的：

```sh
docker compose -p rsshub -f /opt/rsshub/docker-compose.yml up -d \
  --no-deps --force-recreate --pull never rsshub
```

只会重建目标 service，不会 `down`、重建依赖、pull 镜像、删除 volume 或清 Redis。RSSHub 必须重建是因为进程只在启动时读取环境变量。

迁移中会短暂创建 root-only 事务备份；新 env、Compose 校验、容器健康和 provider 复检全部成功后，临时备份会被删除，避免 Cookie 长期保留多份。失败则恢复原文件并重建目标 service。

因此，答案是：安装器确实会修改原来的 `docker-compose.yml`，但仅把这三项从 YAML 迁移到 raw env_file，其他配置不改；成功后不会把原始含 Cookie 的临时备份长期留在服务器。

### 6.2 全新 RSSHub，没有 Cookie

如果目标 Compose 没有这三项，安装器会创建空的 raw env 文件并接入目标 service。首次 bootstrap 只验证 Compose 和 RSSHub 健康，不会用空 Cookie 请求 provider，并把实例标记为尚未 seed。

之后在 Edge 登录并点击“立即同步”：第一个有效 provider 会先预检，再作为首个 live 值应用；另一个 provider 没有更新时不会被清空。无效 Cookie、空值或临时网络错误不会覆盖已有值。新装 RSSHub 的人不需要手动复制 Cookie 或编辑 env。

## 7. Bark（可选）

不配置 Bark 也不影响同步和自愈，只是不推送。服务端配置必须是 `https://api.day.app`。

在服务器上以 root 运行：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json configure-bark
```

程序等待一行输入；只在交互提示中粘贴完整 Bark URL 或单独 Device Key，输入不会回显，回车后退出。不要使用 `printf`、命令行参数、环境变量或脚本传入真实 Key。输出应只有类似：

```json
{"configured":true}
```

通知使用 HTTPS `POST /push`，Device Key 放 JSON body，不放 URL、日志或 RSSHub 容器。测试通知：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json notify-test
```

## 8. 带外核对 SSH 主机密钥

Native Host 强制 `StrictHostKeyChecking=yes`。不能把未经核对的 `ssh-keyscan` 输出直接当作信任根。

通过服务器控制台或已有可信管理会话，在服务器上获取 Ed25519 指纹：

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

在 Mac 获取同一端点的公开条目并计算指纹：

```sh
ssh-keyscan -p 22 -t ed25519 rsshub.example.com > /tmp/rsshub-cookie-sync-known_hosts
ssh-keygen -lf /tmp/rsshub-cookie-sync-known_hosts -E sha256
```

只有两个指纹完全一致时，才把该文件交给 Native Host。非 22 端口要使用实际端口，条目应为 `[rsshub.example.com]:<PORT>`。也可以让管理员通过可信渠道传递公开条目后本地核对。不要使用通配符、哈希 host 条目或 `StrictHostKeyChecking=no`。

## 9. 安装 macOS Native Host

先安装扩展，在 `edge://extensions` 复制扩展卡片显示的 ID。然后在项目目录执行。

### 9.1 直连

```sh
python3 native-host/install.py \
  --extension-id <EDGE_EXTENSION_ID> \
  --server-host rsshub.example.com \
  --server-port 22 \
  --server-user rsshub-sync \
  --known-hosts-source /tmp/rsshub-cookie-sync-known_hosts
```

`--extension-id`、`--server-host` 必填；端口默认 22，用户默认 `rsshub-sync`。没有代理参数时默认直连，Host 不读取 `http_proxy`、`https_proxy` 或 `all_proxy`。

### 9.2 SOCKS5

```sh
python3 native-host/install.py \
  --extension-id <EDGE_EXTENSION_ID> \
  --server-host rsshub.example.com \
  --server-port 22 \
  --server-user rsshub-sync \
  --known-hosts-source /tmp/rsshub-cookie-sync-known_hosts \
  --proxy-host 127.0.0.1 \
  --proxy-port 1080
```

`--proxy-host` 和 `--proxy-port` 必须同时提供。它们只用于本机到服务器的 SSH；服务器访问 provider 使用自己的网络。代理失败会报告失败，不会回退直连。

安装器不连接服务器、不自动 `ssh-keyscan`，会在当前用户目录生成专用 Ed25519 key：

```text
~/Library/Application Support/RSSHub Cookie Sync/
```

Edge 用户级 manifest 是：

```text
~/Library/Application Support/Microsoft Edge/NativeMessagingHosts/com.jayden.rsshub_cookie_sync.json
```

安装器输出的公钥不是秘密。为了不把它放进命令行，使用公钥文件 stdin 传到服务器：

```sh
ssh -p 22 root@rsshub.example.com \
  /usr/local/sbin/rsshub-cookie-sync-provision-key \
  < "$HOME/Library/Application Support/RSSHub Cookie Sync/ssh/id_ed25519.pub"
```

上面的远程命令不含公钥内容，公钥通过 stdin 传输。`root` 是示例管理员账号；如果使用其他管理账号，让其在服务器上以 root 运行固定 wrapper。provision 脚本只接受一行 `ssh-ed25519` 公钥，并原子替换专用 `rsshub-sync` 账号的受限 `authorized_keys`；首版因此只管理一个采集端，重新 provision 会撤销旧采集端。不要给该账号 Docker 组权限，也不要附加任意远程命令。

## 10. 安装 Edge 扩展

Edge 的“加载解压缩的扩展”需要目录，不是 ZIP 文件。Release ZIP 必须先解压。

### 10.1 GitHub Release

1. 在 GitHub Releases 下载 `rsshub-cookie-sync-extension.zip`。
2. 解压到仅当前用户可写的目录：

   ```sh
   mkdir -p "$HOME/Applications/rsshub-cookie-sync-extension"
   unzip -q ~/Downloads/rsshub-cookie-sync-extension.zip \
     -d "$HOME/Applications/rsshub-cookie-sync-extension"
   ```

3. 打开 `edge://extensions`，开启“开发人员模式”。
4. 点击“加载解压缩的扩展”，选择解压目录；目录根部应直接有 `manifest.json`。
5. 复制扩展 ID，按上一节安装 Native Host。

### 10.2 源码

在 `edge://extensions` 中选择“加载解压缩的扩展”，载入仓库的 `extension/` 目录。源码和 Release 使用相同的公开 manifest key，正常情况下 ID 不变。更新后在扩展卡片点击“重新加载”。

### 10.3 授权和同步

在扩展弹窗中：

1. 点击“授权站点权限”，只授予知乎和微博精确 host 权限。
2. 确认 Edge Default Profile 已登录两个站点。
3. 点击“立即同步”。扩展只读取适用于以下实际请求的 Cookie：

   ```text
   https://www.zhihu.com/api/v3/moments
   https://m.weibo.cn/feed/group
   ```

4. `candidate_saved` 表示候选预检通过并待命，`promoted` 表示已切换 live，`unchanged` 表示无需改变，`rejected_invalid` 表示无效，`retryable_error` 表示稍后重试。

“刷新状态”是只读操作：重新读取扩展后台已经保存的最新同步结果和当前站点权限，不读取或上传 Cookie，也不会主动查询服务器。弹窗状态旧时先点“刷新状态”；要查看服务器 timer 刚产生、但尚未经过下一次扩展同步的状态，请在服务器运行 `status --json`。需要采集登录态时才点“立即同步”。扩展只持久化启用开关、SHA-256 指纹、时间、固定状态码和权限结果，不保存 Cookie 原文。

## 11. 验收

服务器：

```sh
systemctl is-enabled rsshub-cookie-sync-monitor.timer
systemctl is-active rsshub-cookie-sync-monitor.timer
systemctl list-timers rsshub-cookie-sync-monitor.timer

/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json status --json

docker compose -p rsshub -f /opt/rsshub/docker-compose.yml config --quiet
docker compose -p rsshub -f /opt/rsshub/docker-compose.yml ps
```

`config --quiet` 成功时无输出。不要运行不带 `--quiet` 的 `config`，它可能展开环境变量。

客户端验收：扩展启用且无错误；Native manifest 的 `allowed_origins` 精确匹配扩展 ID；known_hosts 与服务器指纹一致；公钥已 provision；“立即同步”后扩展显示脱敏结果、服务端出现候选接收/预检时间；“刷新状态”能显示后台最新结果且不触发 Cookie 读取或上传。

可选 Bark 验收：`notify-test` 收到手机推送。journald 只能有状态码、耗时和分类原因，不得出现 Cookie、请求头、Bark Key 或完整响应体。

## 12. 日常状态和监控

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json status --json

/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json monitor --json

journalctl -u rsshub-cookie-sync-monitor.service -n 100 --no-pager
systemctl list-timers rsshub-cookie-sync-monitor.timer
```

`ok` 是探针通过；`auth_failed` 是明确认证失败；`transient` 是网络/限流/超时/服务端错误，不换 Cookie；`transaction_pending` 表示有未完成事务，应先看日志，不要删 transaction 文件。服务端本身每 15 分钟运行一次，Edge Cookie 变化自动等待约 2 分钟后上传。

## 13. 升级、重装和更换部署

### 13.1 同一实例升级/重装

使用完全相同的参数重跑：

```sh
cd /opt/rsshub-cookie-sync
server/install.sh \
  --compose-file /opt/rsshub/docker-compose.yml \
  --project-name rsshub \
  --service-name rsshub \
  --rsshub-base-url http://127.0.0.1:1200
```

已经迁移完成的同一 deployment 会保留 live Cookie、候选、Bark、状态和 SSH 账号，不会把 Cookie 写回 Compose，也不会因为重装无条件重建 RSSHub。首次迁移未完成时才继续处理待定事务。不要删除 `secrets` 或状态目录，不要手动编辑 Compose。

### 13.2 改 Compose 路径/project/service/健康地址

这些参数决定操作哪一个实例。确实要重定向时，先核对新实例，再显式加 `--replace-deployment`：

```sh
server/install.sh \
  --compose-file /opt/another-rsshub/docker-compose.yml \
  --project-name another-rsshub \
  --service-name rsshub \
  --rsshub-base-url http://127.0.0.1:1300 \
  --replace-deployment
```

没有该选项时配置不一致应被拒绝，避免路径写错。重定向不会自动删除旧 Compose、容器或 env。

### 13.3 换服务器

在新服务器安装服务端，带外核对新的 Ed25519 指纹；在 Mac 为新端点准备精确 known_hosts；重新运行 Native Host 并传入新的 `--server-host`、`--server-port`、`--server-user`、`--known-hosts-source` 及代理参数；把新的本机专用公钥通过 stdin provision；重新加载扩展并点击“立即同步”。扩展 ID 通常无需变化。

## 14. 暂停和卸载

暂停/恢复 timer：

```sh
systemctl disable --now rsshub-cookie-sync-monitor.timer
systemctl enable --now rsshub-cookie-sync-monitor.timer
```

服务器卸载（root）：

```sh
server/uninstall.sh
```

它会停用 timer，移除同步器、配置、sudoers、sshd drop-in、专用账号和授权 key，但保留当前 `secrets/rsshub.env`、已迁移 Compose、RSSHub 容器/volume 和状态目录，让 RSSHub 继续静态运行；不会把 secret 重新写回 Compose。确认不需要状态后再按自己的备份策略清理。

如果是“先卸载再安装”，服务端现有 live env、候选和状态仍会被识别，但因为卸载已删除 `/etc/rsshub-cookie-sync/config.json` 与专用账号，你需要重新配置 Bark、重新安装 Native Host 公钥。普通升级不要先卸载，直接用相同参数重跑安装器即可。

客户端卸载：在 `edge://extensions` 移除扩展，然后运行：

```sh
python3 native-host/uninstall.py
```

Native 卸载器只删除它安装的 manifest、Host、配置和默认 SSH 文件，不递归删除任意目录，不触碰 Application Support 之外的自定义密钥。

## 15. 排障

### 弹窗状态旧

在 `edge://extensions` 点击扩展“重新加载”，重新打开弹窗后点“刷新状态”。刷新不上传 Cookie；需要更新登录态才点“立即同步”。

### 权限/Cookie 读取不到

确认已授权 `zhihu.com`、`www.zhihu.com`、`weibo.cn`、`m.weibo.cn` 精确 host，且登录使用 Edge Default Profile；微博保持 `m.weibo.cn` 移动站点。不要从剪贴板补 Cookie。

### Native Host 或 SSH 失败

确认 manifest 文件名为 `com.jayden.rsshub_cookie_sync.json`，`allowed_origins` ID 完全一致，Host 路径是当前用户拥有的绝对路径，known_hosts 指纹正确，服务器已运行安装器并 provision 公钥。显式 SOCKS5 失败不会直连回退；不要关闭 host key 校验。

### `rejected_invalid`、`429`、`432`、超时、`5xx`

`rejected_invalid` 不改变 live env、不重建 RSSHub；重新登录后点“立即同步”。`429`、`432`、超时和 `5xx` 是临时上游/网络错误，检查服务器 DNS、出口、限流和防火墙，不要立即更换 Cookie。

### Compose 或容器失败

```sh
docker compose -p rsshub -f /opt/rsshub/docker-compose.yml config --quiet
docker compose -p rsshub -f /opt/rsshub/docker-compose.yml ps
journalctl -u rsshub-cookie-sync-monitor.service -n 200 --no-pager
```

不要把不带 `--quiet` 的 Compose 输出发到 Issue，不要删除 `.pre-cookie-sync`、`.txn.json` 或 `.prev`；让事务恢复逻辑处理，必要时只提供脱敏日志。

## 16. 测试、打包和 CI

CI 不是安装前提。GitHub Actions 会在 push、Pull Request 和手动触发时运行跨 Python 版本测试并校验扩展 ZIP；发布严格的 `vX.Y.Z` 版本 tag 会生成 `rsshub-cookie-sync-extension.zip` Release asset。

```sh
make check
make package-extension
unzip -l dist/rsshub-cookie-sync-extension.zip
```

测试覆盖 Edge URL/Cookie/刷新逻辑、Native Messaging 帧和 SSH 参数安全、服务端输入限制/候选预检/临时错误/Compose raw env 迁移/事务回滚/通知去重。扩展打包使用运行文件白名单，不包含测试、文档、`node_modules` 或本地构建目录。

提交前检查：

```sh
git diff --check
git status --short
rg -n --hidden --glob '!.git/**' \
  'ZHIHU_COOKIES=|WEIBO_COOKIES=|TWITTER_AUTH_TOKEN=|BEGIN (OPENSSH|RSA|EC) PRIVATE KEY|device_key|ssh-ed25519 AAAA' .
```

测试只能使用合成值，例如 `sid=test-value`，不能读取浏览器、生产服务器、OpenCookie 或真实 secret。

目录职责：`extension/` 是 Edge MV3 采集端，`native-host/` 是 macOS Native Host，`server/` 是 Linux 服务端和 systemd，`scripts/` 是扩展打包脚本，`.github/` 是 CI/Release workflow。

发现安全问题时，不要在公开 Issue 中粘贴 Cookie、Bark Key、SSH key、日志或完整 Compose 输出，按 [SECURITY.md](SECURITY.md) 报告。

## 17. 许可证

本项目使用 MIT License，见 [LICENSE](LICENSE)。
