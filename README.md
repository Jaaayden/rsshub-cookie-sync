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

下面的流程适合刚安装 RSSHub 的用户。普通情况下不需要填写 Compose project、Docker service、RSSHub 地址，也不需要手动编辑 Compose 文件。

### 第一步：在 RSSHub 服务器上安装服务端

以 root 登录 RSSHub 服务器，执行这一条命令：

> 如果这是你第一次通过网络 SSH 连接这台服务器，请先通过云厂商控制台等独立可信渠道核对 SSH 主机指纹，不要直接接受一个未经确认的新 key。已经安全安装好 RSSHub、且 `known_hosts` 中条目正确的用户可直接继续。

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh
```

安装器会下载最新稳定版本，然后打开一个交互式安装流程：

1. 自动查找常见位置的 `docker-compose.yml`，例如当前目录、`/opt/rsshub` 和 `/root/rsshub`；找到后会先让你确认。
2. 默认使用官方 Compose 常见的 `rsshub` 服务名；如果你的文件使用了其他服务名，安装器会列出服务并让你选择。
3. 让 Docker Compose 自己解析 project 名称，不要求你学习这个概念。
4. RSSHub 健康地址自动使用 `http://127.0.0.1:1200`；官方安装通常不需要额外设置。
5. 询问是否配置 Bark，以及是否现在粘贴本机 Native Host 的公钥。暂时没有公钥时可以跳过，之后再执行。

如果服务器上的 Compose 不在这些位置，安装器会提示你输入绝对路径。安装器只在服务器本机工作，不会连接你的 Mac，也不会下载或更新 RSSHub 镜像。

### 第二步：安装 Edge 扩展

在 Mac 上打开 [GitHub Releases](https://github.com/Jaaayden/rsshub-cookie-sync/releases)，下载 `rsshub-cookie-sync-extension.zip`，解压到一个固定目录。

在 Edge 中：

1. 打开 `edge://extensions`；
2. 打开“开发人员模式”；
3. 点击“加载解压缩的扩展”；
4. 选择解压后直接包含 `manifest.json` 的目录。

扩展使用固定的公开 ID。只要没有修改 `manifest.json` 中的 `key`，升级扩展时 ID 不会变化。

### 第三步：安装本机桥接程序

在 Mac 终端执行：

```sh
git clone https://github.com/Jaaayden/rsshub-cookie-sync.git ~/rsshub-cookie-sync
cd ~/rsshub-cookie-sync
python3 native-host/install.py
```

安装器不需要服务器地址参数。它会：

- 安装 Edge Native Messaging Host；
- 默认在 `~/.ssh/rsshub-cookie-sync` 创建 Ed25519 密钥；如果该文件已经存在，就复用它；
- 使用 `~/.ssh/known_hosts` 保存 SSH 主机密钥；
- 输出一行公钥，供服务器安装。

私钥不会进入扩展。扩展设置页只能选择 `~/.ssh/` 下的密钥文件名，Native Host 才会在本机读取对应私钥。复用已有密钥时，先执行 `chmod 600 ~/.ssh/<密钥文件名>`，并确认它能在无人值守状态下使用；带口令且依赖人工输入的私钥不适合定时同步。

### 第四步：核对服务器主机密钥

如果你已经用 SSH 安全连接过这台服务器，并且 `~/.ssh/known_hosts` 中已有正确条目，可以跳过本步。

全新连接时，不能把网络上第一次看到的主机密钥直接当作可信密钥。请让服务器管理员在服务器控制台执行：

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

在 Mac 上获取同一地址的公开条目并计算指纹：

```sh
ssh-keyscan -t ed25519 -p <SSH端口> <服务器地址> > /tmp/rsshub-cookie-sync-known_hosts
ssh-keygen -lf /tmp/rsshub-cookie-sync-known_hosts -E sha256
```

只有两个指纹通过独立可信渠道完全一致时，才把公开条目加入本机文件：

```sh
cat /tmp/rsshub-cookie-sync-known_hosts >> ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts
```

更完整的主机密钥说明见 [高级 SSH 说明](docs/advanced-ssh.md)。

### 第五步：把公钥交给服务器

完成上一步的指纹核对后，Native Host 安装器输出的是公钥，不是私钥。使用已经核对过主机身份的管理员 SSH 连接，把公钥通过标准输入交给服务器：

```sh
ssh -p <管理员SSH端口> root@<服务器地址> \
  /usr/local/sbin/rsshub-cookie-sync-provision-key \
  < ~/.ssh/rsshub-cookie-sync.pub
```

如果你复用了其他密钥，把最后的路径换成对应的 `.pub` 文件。命令中不会出现公钥内容；私钥永远不离开 Mac。也可以在服务器安装器询问时，直接粘贴公钥的一整行。

### 第六步：在扩展中填写连接信息

打开扩展弹窗，点击“连接设置”：

- “服务器地址”：填写运行 RSSHub 的域名或 IPv4 地址；
- “SSH 端口”：通常是 `22`；
- “SSH 密钥文件名”：选择 `~/.ssh/` 中与服务器公钥对应的密钥。

点击“保存连接设置”。这些连接信息保存在本机 Native Host，不写入扩展存储；私钥内容不会被扩展读取或上传。服务器端账号由安装器创建为 `rsshub-sync`，不需要另外填写。

### 第七步：授权并同步

1. 在 Edge Default Profile 登录 `https://www.zhihu.com` 和 `https://m.weibo.cn`。
2. 在扩展中点击“授权站点权限”，只允许知乎和微博的精确站点权限。
3. 点击“立即同步”。

第一次同步时，服务器会分别验证两个 Cookie。有效 Cookie 会保存为候选；全新 RSSHub 会在安全事务完成后使用它。某一个 provider 失败不会清空另一个 provider。

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

再次运行同一条一键命令即可。安装器会识别已经迁移的实例，自动复用原来的 project、service 和非默认本机健康端口，并保留当前 Cookie、候选、Bark、状态和受限 SSH 账号；不会因为“重新安装”而无条件重建 RSSHub。

不要把 Cookie 手动写回 `docker-compose.yml`，也不要删除 `secrets/rsshub.env` 来“重置”。

如果以后要卸载服务端，运行 `sudo /usr/local/sbin/rsshub-cookie-sync-uninstall`。它会停用同步器，但保留最后一次 live secret env，让 RSSHub 继续静态运行；完整边界见 [服务端卸载说明](server/README.md#卸载)。

### 换 Compose 路径或非官方布局

普通安装不需要 `--project-name`、`--service-name` 或 `--rsshub-base-url`。只有 Compose 文件不是常见布局、服务名不是 `rsshub`，或同一服务器管理多套 RSSHub 时，才需要在高级文档中使用兼容参数：

- [Compose 高级说明](docs/advanced-compose.md)
- [服务端安装说明](server/README.md)

### 换服务器

在新服务器重新运行一键安装器；在 Mac 的扩展“连接设置”中换成新地址和端口，选择对应密钥；核对新服务器的 Ed25519 主机指纹，并把本机公钥重新 provision。扩展 ID 不需要改变。

## 查看状态和常见问题

服务器上的服务端状态是脱敏 JSON，不包含 Cookie、Bark Key 或完整响应：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub_cookie_sync.py \
  --config /etc/rsshub-cookie-sync/config.json status --json
```

常见问题：

- 弹窗显示旧状态：点击“刷新扩展状态”；如果要重新采集登录态，再点击“立即同步”。
- `需授权`：点击“授权站点权限”，并确认登录的是 Edge Default Profile。
- Native Host 不可用：确认已经运行 `python3 native-host/install.py`，然后在 `edge://extensions` 重新加载扩展。
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

CI 不是安装前提。GitHub Actions 会运行测试并在版本 Release 中生成可直接加载的扩展 ZIP 和一键服务端安装脚本。

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
