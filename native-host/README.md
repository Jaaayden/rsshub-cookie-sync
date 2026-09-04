# RSSHub Cookie Sync Native Messaging Host

[返回项目总览](../README.md)

Native Host 是 Edge 扩展和 RSSHub 服务器之间的本机桥接程序。它使用 Python 3.9+ 标准库，不需要安装第三方包，也不会保存 Cookie。

## 安装

新手主流程中先在 Mac 准备本机桥接程序，再安装 Edge 扩展。普通安装直接执行本机一键 bootstrap：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh
```

bootstrap 会下载最新稳定版本并调用 Native Host 安装器；状态写入终端错误输出，最后一行标准输出是待粘贴到服务端安装器的公钥。它不会创建项目源码目录，也不需要服务器地址参数。源码安装或调试时才进入仓库执行：

```sh
cd ~/rsshub-cookie-sync
python3 native-host/install.py
```

官方扩展使用固定的公开 ID，所以默认命令不需要 `--extension-id`。普通安装只做本地文件操作：

- 把 Host 安装到当前用户的 Edge Native Messaging 目录；
- 全新安装时只在 `~/.ssh/rsshub-cookie-sync` 创建项目专用 Ed25519 密钥；如果该文件已存在，则复用它；
- 使用 `~/.ssh/known_hosts` 作为 SSH 主机密钥文件；
- 显示一行公钥，供服务器上的 `rsshub-sync` 账号安装。

安装器不会回退到 `id_ed25519` 或其他通用登录密钥，也不会自动覆盖旧密钥。普通重装只复用 Native Host 配置中已经记录的这把精确项目专用密钥；如果检测到旧版或通用密钥，会在替换任何本机文件前停止，并提示执行两阶段迁移。扩展设置页虽然保留了选择其他已有 Ed25519 的高级兼容入口，但不建议选择可能同时登录 `root` 或其他服务器的密钥。

项目专用私钥必须是当前用户拥有的普通文件，权限通常应为 `0600`（可执行 `chmod 600 ~/.ssh/rsshub-cookie-sync`），并且必须能在无人值守状态下使用；带口令且依赖人工输入的私钥不适合定时同步。对应的 `~/.ssh/rsshub-cookie-sync.pub` 是授权服务器时使用的公钥，私钥永远不上传。

普通安装和 `--prepare-dedicated-key` 不会连接服务器、读取浏览器 Cookie 或运行 Docker。只有用户明确运行 `--activate-dedicated-key` 时，安装器才会进行一次不含 Cookie 的 SSH 认证探测。

## 新手首次连接

Host 使用严格的 SSH 主机密钥校验。新手不需要手动运行 `ssh-keyscan`、编辑 `known_hosts` 或执行单独的公钥授权命令。

运行本安装器并复制它显示的 `ssh-ed25519` 公钥后，从 Mac 普通 SSH 登录服务器一次：

```sh
ssh -p <服务器SSH端口> root@<服务器地址>
```

首次连接时，确认这是你要连接的服务器后，在 SSH 提示中输入 `yes`。SSH 会把主机条目保存到 `~/.ssh/known_hosts`。保持这个 root shell 不要退出，在同一个会话中运行服务端一键安装器；当安装器要求粘贴 Native Host 公钥时，粘贴刚才复制的整行公钥。公钥通过服务端安装器的标准输入传递，私钥永远不离开 Mac。

主机指纹的独立核对、手工 `known_hosts` 和单独 provision 仅适用于高级场景，见[高级 SSH 说明](../docs/advanced-ssh.md)。

服务器端一次只保留一个采集端公钥。重新执行 provision 会替换旧公钥，因此新手首次安装不应单独调用它；更换密钥或补救跳过的授权时才按高级说明操作。

## 旧版或通用密钥迁移

如果已有 Native Host 配置指向 `id_ed25519` 等旧版/通用密钥，普通重装不会自动替换它，也不会写入新配置。请明确分两阶段迁移，确保服务器先接受新公钥：

```sh
# 阶段 1：创建/检查专用密钥；当前 Host 配置保持不变
python3 native-host/install.py --prepare-dedicated-key

# 用管理员账号授权专用公钥（命令从标准输入读取公钥）
ssh -p <管理员SSH端口> root@<服务器地址> \
  /usr/local/sbin/rsshub-cookie-sync-provision-key \
  < ~/.ssh/rsshub-cookie-sync.pub

# 阶段 2：明确激活专用密钥
python3 native-host/install.py --activate-dedicated-key
```

第二阶段会保留旧配置中的服务器地址和端口，并先用不含 Cookie 的空请求做一次 SSH 认证探测。只有专用公钥已被服务器接受、且 `known_hosts` 校验通过，才会把 Native Host 配置切换到 `~/.ssh/rsshub-cookie-sync`；探测失败时本机旧配置和已安装文件保持不变。完成后在扩展“连接设置”选择该密钥并保存，再点击“立即同步”。

服务器授权程序一次只保留一个同步公钥：授权新公钥后，旧私钥会立即失去 `rsshub-sync` 访问权，因此应紧接着运行激活命令。如果激活探测失败，需要用管理员连接重新 provision 旧公钥，才能恢复旧连接；本机旧配置本身不会被安装器覆盖。

## 扩展设置页

打开扩展弹窗，点击“连接设置”，填写：

- 服务器地址：运行 RSSHub 的域名或 IPv4 地址；
- SSH 端口：通常是 `22`；
- SSH 密钥文件名：普通安装选择 `~/.ssh/rsshub-cookie-sync`，并确认它与服务器上的公钥是一对。

连接账号由服务端安装器固定创建为 `rsshub-sync`，扩展中只读显示、不允许修改。点击“保存连接设置”后，地址、端口和所选密钥名保存到 Native Host 的本机配置文件，不进入扩展存储。Native Host 只把私钥路径交给本机系统 SSH 来建立连接。

高级用户可以在扩展设置中手动选择 `~/.ssh/` 下其他已有的 Ed25519 密钥，但安装器不会为这类密钥创建或自动迁移配置。只有在确认该密钥不同时用于 `root` 或其他服务器时才应使用；通用密钥会扩大 Cookie 同步链路泄露后的影响范围。

设置页的“重新读取设置”只从 Native Host 读取连接配置，不会读取 Cookie 或连接服务器。弹窗中的“刷新扩展状态”只更新本机脱敏同步结果；要重新采集 Cookie，请点击“立即同步”。

## Native Messaging 数据流

扩展使用 Chromium Native Messaging 的 4 字节 little-endian 长度帧。同步请求的形状固定为：

```json
{
  "version": 1,
  "providers": {
    "zhihu": { "cookieHeader": "name=value; other=value" },
    "weibo": { "cookieHeader": "name=value; other=value" }
  }
}
```

Cookie 只在浏览器 API 返回后、Native Host 内存中和 SSH 标准输入中短暂存在。它不会进入 SSH 参数、环境变量、Native Host 配置、扩展持久化存储或日志。服务器强制命令只返回以下状态之一：

```text
unchanged
candidate_saved
promoted
rejected_invalid
retryable_error
```

扩展还可以发送不含 Cookie 的本机设置请求，用于“连接设置”页读取和保存服务器地址及密钥选择。Native Host 会验证字段，只返回脱敏的文件名和状态，不返回私钥路径、密钥内容或异常文本。

## 安装文件

默认位置：

```text
~/Library/Application Support/Microsoft Edge/NativeMessagingHosts/com.jayden.rsshub_cookie_sync.json
~/Library/Application Support/RSSHub Cookie Sync/native_host.py
~/Library/Application Support/RSSHub Cookie Sync/native_host
~/Library/Application Support/RSSHub Cookie Sync/config.json
~/Library/Application Support/rsshub-cookie-sync/uninstall.sh
~/.ssh/rsshub-cookie-sync
~/.ssh/rsshub-cookie-sync.pub
~/.ssh/known_hosts
```

配置和私钥为当前用户私有，Host 清单只允许官方扩展的精确 origin。固定扩展 ID 是公开产品标识，不是签名；只加载本仓库或 GitHub Release 中的扩展。

如果你自行修改扩展 `manifest.json` 的 `key`，扩展 ID 会变化，必须用新 ID 重新安装 Host：

```sh
python3 native-host/install.py --extension-id <新的扩展ID>
```

这是自定义扩展的兼容入口，普通用户不需要使用。

## 升级和卸载

升级或普通重装 Host：优先在 Mac 重新运行本机一键 bootstrap：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh
```

如果当前配置已经使用项目专用密钥，它会复用同一个 `~/.ssh/rsshub-cookie-sync`，不生成替代密钥；不会把 Cookie 写入本机磁盘。若提示发现旧版/通用密钥，请先完成上面的两阶段迁移。升级扩展后，在 `edge://extensions` 点击“重新加载”。只有从源码目录安装或进行调试时，才直接运行 `python3 native-host/install.py`；它不是普通重装入口。

卸载本机 Host（服务端和 Edge 已按项目首页顺序处理后）：

```sh
"$HOME/Library/Application Support/rsshub-cookie-sync/uninstall.sh"
```

如果固定入口不存在，也可以使用 macOS curl bootstrap：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh -s -- uninstall
```

Release 同时提供独立 `uninstall-macos.sh` 和 `SHA256SUMS`。该脚本不自行删除任何密钥，只调用安装时写入的版本匹配卸载程序。

这两个入口都会删除 Native Host、Edge manifest 和 Native Host 配置，但默认保留 `~/.ssh/` 中的项目专用密钥和 `known_hosts`。如果是从源码目录安装、且固定入口不可用，才使用下面的兼容命令：

```sh
python3 native-host/uninstall.py
```

源码兼容命令同样不会删除项目密钥。不要为卸载递归删除用户目录。

## 测试

```sh
python3 -B -m unittest discover -s native-host -p 'test_*.py' -v
```

测试不会连接真实服务器、读取浏览器 Cookie 或使用真实私钥。
