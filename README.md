# RSSHub Cookie Sync

[![CI](https://github.com/Jaaayden/rsshub-cookie-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/Jaaayden/rsshub-cookie-sync/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Jaaayden/rsshub-cookie-sync)](https://github.com/Jaaayden/rsshub-cookie-sync/releases)

RSSHub Cookie Sync 会把 Microsoft Edge 中已经登录的知乎、微博登录态同步到 RSSHub。它会定期检查登录态，在 Cookie 失效时使用已经验证过的新 Cookie 自动修复，并在需要人工登录时通过 Bark 提醒。

它不修改 RSSHub 源码、不需要 CookieCloud，也不会因为一段时间没有新文章就误报登录失效。

```text
Edge 扩展 → 本机 Native Host → SSH → RSSHub 服务器
                                      ├─ 验证 Cookie
                                      ├─ 安全更新 Compose env_file
                                      └─ 定时检查并通过 Bark 告警
```

> Cookie 等同于登录凭证。不要把 Cookie、Bark Device Key、SSH 私钥、真实服务器地址或完整 Compose 输出发到 Issue、聊天、截图或 Git 仓库。

## 最简单的安装方式

下面是给新手的完整顺序。普通情况下不需要填写 Compose project、Docker service 或 RSSHub 地址，也不需要手动编辑 Compose 文件。

### 第一步：在 Mac 安装本机桥接程序并复制公钥

在 Mac 终端以当前普通用户执行本机一键安装：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh
```

安装器不需要服务器参数，也不会创建项目源码目录。全新安装只会在 `~/.ssh/rsshub-cookie-sync` 创建项目专用 Ed25519 密钥；如果这个文件已经存在，就只复用这一把精确的项目密钥。它不会回退到 `id_ed25519` 或其他通用登录密钥，也不会覆盖通用密钥。

安装器会把状态写到终端错误输出，并将一行公钥作为最后的标准输出。复制这一整行，稍后粘贴到服务端安装器；复制的是 `.pub` 公钥，不是私钥。不要运行单独的 `provision-key` 命令。扩展日常连接使用固定的受限账号 `rsshub-sync`，不是 `root`。如果你要从源码安装或调试，见 [Native Host 说明](native-host/README.md)。

### 第二步：普通 SSH 登录一次，并在同一个 root 会话安装服务端

从 Mac 执行一次普通 SSH 登录：

```sh
ssh -p <服务器SSH端口> root@<服务器地址>
```

首次连接时 SSH 会显示主机指纹并询问是否继续。确认这是你要连接的服务器后输入 `yes`，让 SSH 将主机条目保存到 `~/.ssh/known_hosts`。本流程不需要手动运行 `ssh-keyscan`、编辑 `known_hosts` 或准备临时文件；如果你需要独立核对指纹，见[高级 SSH 说明](docs/advanced-ssh.md)。

保持这个 root shell 不要退出，在同一个会话中执行：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh
```

服务端安装器会自动查找常见位置的 Compose 文件；普通官方布局直接确认即可。它默认使用 `rsshub` service 和 `http://127.0.0.1:1200`，并让 Docker Compose 自动解析 project 名称。安装过程中会要求粘贴第一步复制的那一整行公钥，也可以配置 Bark。服务端会在这一步自动创建 `rsshub-sync` 受限账号并安装公钥，不需要另开终端或单独执行授权命令。

如果 Compose 文件不在常见位置，安装器会在交互提示中要求输入绝对路径。它不会下载或更新 RSSHub 镜像，也不会连接你的 Mac。

### 第三步：安装 Edge 扩展

回到 Mac，打开 [GitHub Releases](https://github.com/Jaaayden/rsshub-cookie-sync/releases)，下载 `rsshub-cookie-sync-extension.zip`，解压到固定目录。

在 Edge 中：

1. 打开 `edge://extensions`；
2. 打开“开发人员模式”；
3. 点击“加载解压缩的扩展”；
4. 选择解压后直接包含 `manifest.json` 的目录。

扩展使用固定的公开 ID。只要没有修改 `manifest.json` 中的 `key`，升级扩展时 ID 不会变化。

Release 同时提供 `SHA256SUMS`。如果你下载了扩展 ZIP 和该校验文件，可在同一目录执行：

```sh
grep ' rsshub-cookie-sync-extension.zip$' SHA256SUMS | shasum -a 256 -c -
```

输出 `OK` 才表示下载文件与 Release 中的校验值一致。

### 第四步：确认 Native Host、填写连接设置并同步

打开扩展，进入“连接设置”，点击“重新读取设置”。这个按钮会通过 Native Host 读取本机连接配置；能够成功显示设置，才表示 Native Host 已安装且可用。弹窗里的“刷新扩展状态”不是 Host 检查：它只重新读取后台已经保存的本地脱敏状态，不调用 Native Host、不读取 Cookie，也不会上传或启动 SSH。

- “服务器地址”：填写运行 RSSHub 的域名或 IPv4 地址；
- “SSH 端口”：通常是 `22`；
- “SSH 密钥文件名”：选择 `rsshub-cookie-sync`；它必须与刚才粘贴并授权的 `.pub` 是一对；
- “SSH 用户名”：只读显示为 `rsshub-sync`，不能改成 `root`。

点击“保存连接设置”。再在同一个 Edge Default Profile 登录 `https://www.zhihu.com` 和 `https://m.weibo.cn`，点击“授权站点权限”，最后点击“立即同步”。如果弹窗显示刚才的旧结果，再点一次“刷新扩展状态”；重新读取网站登录态则使用“立即同步”。

扩展设置页允许高级用户手动选择其他已有的 Ed25519，但安装器不会为它们创建或自动迁移配置。只有确认该密钥没有同时用于 `root` 或其他服务器时才应使用；普通安装始终使用项目专用的 `rsshub-cookie-sync`。

## 哪些内容会配置，哪些不会写死

仓库和扩展包中不包含任何人的服务器地址、端口、Cookie、Bark Key 或 SSH 私钥。每一台设备自己的值分别保存在这里：

| 内容 | 配置方式与保存位置 |
| --- | --- |
| 服务器地址、SSH 端口、所选密钥文件名 | 扩展“连接设置”填写，由 Mac 上的 Native Host 保存 |
| SSH 私钥 | 保留在当前用户的 `~/.ssh/`，扩展只能看到安全的文件名 |
| Compose 文件、project、service | 服务端安装器自动发现并让 Docker Compose 解析；非标准部署才手工指定 |
| RSSHub 健康地址 | 默认 `http://127.0.0.1:1200`，普通安装不询问 |
| Bark Device Key | 服务端交互输入，保存在 root-only 配置中 |
| 知乎、微博 Cookie | 从 Edge 临时读取；live 值只保存在服务器的 root-only env 中 |

`rsshub-sync` 是服务端安装器专门创建的固定受限账号，不是你的管理员账号，也不能获得普通 shell。固定账号名可以让 forced command 和权限边界保持一致；它不包含个人凭据。

## 扩展里的几个按钮

- “立即同步”：读取当前 Edge 登录态并上传。Cookie 只在当前调用的内存和 SSH 标准输入中短暂存在。
- “刷新扩展状态”：只重新读取后台已经保存的状态并重新渲染页面，不读取 Cookie、不上传、不启动 SSH。弹窗长时间没有打开时，先点它即可看到最新本地状态。
- “复制 Cookie”：只有在你明确点击某一个 provider 的按钮、确认警告并授权剪贴板后才会复制该 provider 的 Cookie。这是为了保留手动应急能力的例外；复制后请按下面的隐藏输入流程使用，并及时清理剪贴板。
- 右上角开关：暂停或恢复自动同步。暂停不影响已经安装在服务器上的监控。

自动同步在 Edge 启动、每 15 分钟以及目标 Cookie 变化后触发。Cookie 变化会等待约 2 分钟合并，避免短时间内重复上传。Edge 关闭或 Mac 睡眠时不会采集，但服务器监控仍会运行。

### 手动应急更新 Cookie

先在扩展中复制对应服务的 Cookie，再以 root 登录服务器并运行其中一条：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync manual-update --provider zhihu
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync manual-update --provider weibo
```

在提示后粘贴并按回车，内容不会回显。这个入口不会直接改 Compose，而是走与自动同步相同的格式校验、上游验证、候选保存、必要时切换和失败回滚；终端输出也不会包含 Cookie。不要把 Cookie 写进命令参数。完成后按自己的系统策略清理剪贴板。

## Compose 会不会被修改？

会，但只在首次接入时做一次有范围的迁移。安装器会处理目标 RSSHub service 中的三项：

```text
ZHIHU_COOKIES
WEIBO_COOKIES
TWITTER_AUTH_TOKEN
```

它会把这三项移到 Compose 文件旁的 `secrets/rsshub.env`，并在目标 service 中加入：

```yaml
env_file:
  - path: ./secrets/rsshub.env
    format: raw
```

`format: raw` 可以避免 Cookie 中的 `$`、`#` 等字符被 Compose 重新解释。`secrets` 目录为 `0700`，env 文件为 `0600`，Compose 文件也会收紧为 `0600`。

其他容器、端口、卷、网络、普通环境变量和镜像版本不会被迁移逻辑主动改写。安装器只重建目标 RSSHub service，不会 `docker compose down`、重建 Redis 或 browserless、拉取镜像、删除 volume，也不会清空 Redis。

如果新安装的 RSSHub 还没有这三项，安装器会创建空的 raw env 文件并接入 Compose；之后由 Edge 第一次同步有效 Cookie。安装成功后，运行中的 Cookie 不会再写回 Compose。

## 重装、升级和更换服务器

### 同一台服务器再次安装

同一台服务器的普通重装或升级，优先在 Mac 再次运行本机一键 bootstrap：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh
```

安装器会复用配置中这把精确的项目专用密钥，不生成新密钥、不改写服务器地址和主机信任文件，也不会因为“重新安装”而无条件重建 RSSHub。它会在替换本机 Host、启动器或配置前完成校验；校验失败时会保留原安装。只有从源码目录安装或进行调试时，才按 [Native Host 说明](native-host/README.md) 使用源码兼容命令。

如果旧版配置使用了 `id_ed25519` 或其他旧版/通用密钥，普通重装会明确停止并且不写配置。请使用下面的两阶段迁移：

```sh
# 阶段 1：只创建/检查项目专用密钥，不改当前 Native Host 配置
python3 native-host/install.py --prepare-dedicated-key

# 使用管理员 SSH，把上一步生成的公钥授权给服务器 rsshub-sync
ssh -p <管理员SSH端口> root@<服务器地址> \
  /usr/local/sbin/rsshub-cookie-sync-provision-key \
  < ~/.ssh/rsshub-cookie-sync.pub

# 阶段 2：明确激活专用密钥；会保留旧配置中的服务器地址和端口
python3 native-host/install.py --activate-dedicated-key
```

授权完成后，第二阶段会先用不含 Cookie 的空请求做一次 SSH 认证探测；只有专用公钥和 `known_hosts` 都通过，才会切换本机配置。然后在扩展“连接设置”选择 `rsshub-cookie-sync` 并保存，再点击“立即同步”。如果探测失败，本机旧配置和已安装文件仍保持不变。

注意：服务器一次只保留一个同步公钥，执行 provision 后旧私钥会立即失去 `rsshub-sync` 访问权，因此应紧接着执行激活命令。如果激活失败，需要用管理员连接重新 provision 旧公钥，才能恢复旧连接。

不要把 Cookie 手动写回 `docker-compose.yml`，也不要删除 `secrets/rsshub.env` 来“重置”。

如果以后要卸载服务端，运行 `sudo /usr/local/sbin/rsshub-cookie-sync-uninstall`。它会停用同步器并删除候选 Cookie 和状态，只保留最后一次 live secret env，让 RSSHub 继续静态运行；完整边界见 [服务端卸载说明](server/README.md#卸载)。

### 换 Compose 路径或非官方布局

普通安装不需要 `--project-name`、`--service-name` 或 `--rsshub-base-url`。只有 Compose 文件不是常见布局、服务名不是 `rsshub`，或同一服务器管理多套 RSSHub 时，才需要在高级文档中使用兼容参数：

- [Compose 高级说明](docs/advanced-compose.md)
- [服务端安装说明](server/README.md)

### 换服务器

在新服务器重新运行一键安装器；先从 Mac 普通 SSH 登录一次接受主机提示，再在同一个 root shell 中运行服务端安装器并粘贴项目公钥。然后在 Mac 的扩展“连接设置”中换成新地址和端口，选择 `rsshub-cookie-sync`。扩展 ID 不需要改变；需要单独更换已部署公钥时再参阅高级 SSH 说明。

## 卸载顺序

请按“服务端 → Edge → Mac”的顺序卸载，避免服务端仍在运行而本机采集链路已经消失：

1. 先在服务器执行服务端卸载。已有 root shell 可直接运行：

   ```sh
   /usr/local/sbin/rsshub-cookie-sync-uninstall
   ```

   也可以使用服务端 curl bootstrap：

   ```sh
   curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh -s -- uninstall
   ```

   Release 中的 `uninstall-server.sh` 是同版本的独立备用入口；正常情况优先用已安装的固定命令，因为它必然与当前安装版本一致。

   服务端卸载只停用同步器、删除其程序、候选 Cookie 和 `/var/lib/rsshub-cookie-sync` 状态；只删除确认由本项目创建的 `rsshub-sync` 账号。如果安装前已经存在同名账号，卸载只移除本项目添加的 SSH 授权和相关配置，不删除账号本身。默认只保留 Compose、仍在运行的 RSSHub 和 `secrets/rsshub.env`，不会把 secret 写回 Compose。确认以后不再需要 RSSHub 使用这些 secret 后，再由管理员手工清理。

2. 再在 Edge 打开 `edge://extensions`，找到 RSSHub Cookie Sync，点击“移除”。这只删除浏览器扩展及其本地扩展数据，不会删除服务器文件。

3. 最后在 Mac 执行本机卸载。已通过安装器安装过 Native Host 时，优先使用安装后固定入口：

   ```sh
   "$HOME/Library/Application Support/rsshub-cookie-sync/uninstall.sh"
   ```

   如果固定入口不存在，也可以使用 macOS curl bootstrap：

   ```sh
   curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh -s -- uninstall
   ```

   Release 中的 `uninstall-macos.sh` 也可独立下载执行；它只调用安装时写入的版本匹配卸载程序。

   本机卸载删除 Native Host、Edge manifest 和 Native Host 配置，但默认保留 `~/.ssh/rsshub-cookie-sync`、对应 `.pub` 和 `~/.ssh/known_hosts`。源码安装用户的兼容卸载命令见 [Native Host 说明](native-host/README.md)；不要为卸载递归删除用户目录。

## 查看状态和常见问题

服务器上的服务端状态是脱敏 JSON，不包含 Cookie、Bark Key 或完整响应：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json status --json
```

常见问题：

- 弹窗显示旧状态：点击“刷新扩展状态”；如果要重新采集登录态，再点击“立即同步”。
- `需授权`：点击“授权站点权限”，并确认登录的是 Edge Default Profile。
- Native Host 不可用：在 Mac 重新运行 `curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh`，然后在 `edge://extensions` 重新加载扩展；只有源码安装或调试时才使用 [Native Host 说明](native-host/README.md) 中的源码兼容命令。
- SSH 连接失败：检查扩展“连接设置”、`~/.ssh/known_hosts` 指纹以及服务器上的公钥是否对应；不要关闭主机密钥校验。
- `候选被拒绝`：先在 Edge 重新登录对应网站，再点击“立即同步”。
- 没有 Bark：服务端安装时可以跳过，之后按 [服务端说明](server/README.md) 配置并运行 `notify-test`。

更多故障分类和安全恢复步骤见 [故障排查](docs/troubleshooting.md)。

## 支持范围与安全边界

- 客户端：macOS + Microsoft Edge Chromium + Edge Default Profile；
- 服务端：Linux、systemd、Docker Engine、Docker Compose v2.30+；
- 运行时只使用 Python 3.9+ 标准库；Node.js 仅用于扩展测试；
- 服务端每 15 分钟检查 RSSHub、知乎和微博登录态；连续确认失效后才切换候选；
- `403`、`429`、`432`、超时和 `5xx` 会被当作临时上游故障，不会立即更换 Cookie；
- 本项目不自动输入密码，不绕过验证码或 MFA。

Cookie 最终会进入 RSSHub 容器的进程环境。拥有服务器 root 或 Docker 管理权限的人可以读取它；这属于宿主机信任边界。浏览器扩展只申请两个站点的精确权限，不申请网页脚本注入或全站点权限。

## 开发、测试和打包

CI 不是安装前提。GitHub Actions 会运行测试，并在版本 Release 中生成可直接加载的扩展 ZIP、macOS/服务端一键安装脚本、两端独立卸载入口以及 `SHA256SUMS`。安装器写入的固定卸载程序与它所安装的版本一致；Release 中的 `uninstall-macos.sh` 和 `uninstall-server.sh` 用于独立下载或应急清理。

```sh
make check
make package-extension
```

测试使用合成 Cookie，不会连接生产服务器，也不会读取当前浏览器登录态。

## 相关官方文档

- [Chrome Cookie API](https://developer.chrome.com/docs/extensions/reference/api/cookies)
- [Chrome Permissions API](https://developer.chrome.com/docs/extensions/reference/api/permissions)
- [Microsoft Edge Native Messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging)
- [Docker Compose `env_file`](https://docs.docker.com/reference/compose-file/services/#env_file)
- [Bark 推送文档](https://github.com/Finb/Bark/blob/master/docs/en-us/tutorial.md)

## 许可证

本项目使用 MIT License，见 [LICENSE](LICENSE)。
