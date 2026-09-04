# SSH 高级说明

普通用户只需运行 macOS bootstrap，然后按项目首页流程通过一次普通 SSH 登录完成主机信任，再在扩展“连接设置”填写服务器地址、端口并选择项目专用密钥。本页仅供需要独立指纹核对、手工 `known_hosts`、单独 provision、自定义扩展或旧部署迁移的高级用户使用；新手不需要执行本页命令。

## 默认文件

Native Host 默认使用当前 Mac 用户的：

```text
~/.ssh/rsshub-cookie-sync
~/.ssh/rsshub-cookie-sync.pub
~/.ssh/known_hosts
```

这不是应用私有的密钥目录。已有 `~/.ssh/` 密钥可以在扩展设置页选择；对应的公钥必须已经安装在服务器 `rsshub-sync` 账号中。私钥不会进入浏览器扩展，也不会上传到服务器。自动任务不能弹出密码输入，因此复用的私钥必须能在无人值守状态下使用。

全新安装和普通重装只使用 `~/.ssh/rsshub-cookie-sync`：文件不存在时创建一把项目专用 Ed25519 密钥，文件存在时复用同一个文件。安装器不会回退到 `id_ed25519` 或其他通用登录密钥，也不会在重装时生成替代密钥。若配置已经指向旧版/通用密钥，普通重装会在写入任何 Host、启动器或配置前停止。

`--identity-file` 仅作为兼容参数保留，并且只接受精确路径 `~/.ssh/rsshub-cookie-sync`；它不是选择通用密钥的入口。高级用户如需使用其他已有 Ed25519，必须在扩展“连接设置”手动选择，并先将匹配的 `.pub` 授权给 `rsshub-sync`。这类密钥如果同时能登录 `root` 或其他服务器，会扩大 Cookie 同步链路泄露后的影响范围，不建议使用。

如需指定项目专用密钥路径（例如脚本明确传参），可使用：

```sh
python3 native-host/install.py --identity-file ~/.ssh/rsshub-cookie-sync
```

密钥必须是 `~/.ssh/` 下的普通文件，由当前用户拥有，并且权限通常为 `0600` 或更严格。普通用户不需要这个参数，直接运行无参数安装即可。

## 主机密钥首次核对

Native Host 使用 `StrictHostKeyChecking=yes`，不会自动信任网络返回的主机 key。

服务器管理员在服务器控制台执行：

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

Mac 上获取同一端点的公开条目：

```sh
ssh-keyscan -t ed25519 -p <SSH端口> <服务器地址> > /tmp/rsshub-cookie-sync-known_hosts
ssh-keygen -lf /tmp/rsshub-cookie-sync-known_hosts -E sha256
```

通过独立可信渠道逐字比较两个指纹。确认一致后，把公开条目合并到 `~/.ssh/known_hosts`：

```sh
cat /tmp/rsshub-cookie-sync-known_hosts >> ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts
```

也可以让管理员通过可信渠道提供已经核对过的条目，再使用安装器的兼容参数：

```sh
python3 native-host/install.py \
  --known-hosts-source /tmp/rsshub-cookie-sync-known_hosts
```

`ssh-keyscan` 只负责获取公开条目，不能代替指纹核对。非 22 端口的条目会使用 `[主机]:端口` 格式。Native Host 不自行解析 `known_hosts`，因此 OpenSSH 生成的普通或哈希主机记录，以及 OpenSSH 支持的 Ed25519、ECDSA 或 RSA 主机密钥都可以正常使用。上面的 `-t ed25519` 只是为了让手工指纹比较更明确，不是运行时限制。不要使用通配符条目或关闭主机密钥校验。

## 自定义扩展 ID

官方扩展使用固定公开 ID，Native Host 安装器会自动使用它。只有你自己修改了 `manifest.json` 的 `key` 或安装自己的扩展时，才需要：

```sh
python3 native-host/install.py --extension-id <edge://extensions 显示的32位ID>
```

ID 必须从 Edge 的扩展页面复制，不能凭记忆填写。Native Host manifest 的 `allowed_origins` 会精确限制该 ID。固定 ID 不是扩展签名，请只加载本仓库或 GitHub Release 的文件。

## Native Host 配置

连接设置保存在：

```text
~/Library/Application Support/RSSHub Cookie Sync/config.json
```

它只包含服务器地址、端口、账号、密钥文件路径和主机密钥文件路径，不包含 Cookie。扩展设置页发送的内容会经过 Native Host 严格验证：服务器地址只能是域名或 IPv4 地址，密钥选择只能解析为 `~/.ssh/` 下的一层文件名。首版不接受 IPv6 字面地址。

如需手工检查安装位置：

```text
~/Library/Application Support/Microsoft Edge/NativeMessagingHosts/com.jayden.rsshub_cookie_sync.json
~/Library/Application Support/RSSHub Cookie Sync/native_host
~/Library/Application Support/RSSHub Cookie Sync/native_host.py
~/Library/Application Support/rsshub-cookie-sync/uninstall.sh
```

这些文件由当前用户拥有，配置为 `0600`，目录为 `0700`。不要把配置或私钥加入 Git。

## 服务器端公钥

服务器安装器创建的账号是 `rsshub-sync`，只允许受限的 forced command。确认上面的主机指纹后，再通过已核对的管理员 SSH 连接接入公钥：

```sh
ssh -p <管理员SSH端口> root@<服务器地址> \
  /usr/local/sbin/rsshub-cookie-sync-provision-key \
  < ~/.ssh/rsshub-cookie-sync.pub
```

该账号没有 Docker 组权限，不能获得普通 shell。重新 provision 会替换同步器的旧公钥，适合更换本机密钥或更换 Mac；不会上传私钥。

## 旧版本迁移

旧版本可能把私钥和主机密钥放在 Application Support，或把 `id_ed25519` 等通用密钥写入配置。普通重装检测到非 `~/.ssh/rsshub-cookie-sync` 的旧密钥后会停止，不会写入新配置，也不会替换已安装的 Host 或启动器。

请按以下两阶段迁移。第一阶段只准备新密钥，旧配置保持原样；管理员授权完成后，第二阶段才会切换：

```sh
# 阶段 1：创建/检查项目专用密钥，不修改当前 Host 配置
python3 native-host/install.py --prepare-dedicated-key

# 用已核对主机指纹的管理员连接授权新公钥
ssh -p <管理员SSH端口> root@<服务器地址> \
  /usr/local/sbin/rsshub-cookie-sync-provision-key \
  < ~/.ssh/rsshub-cookie-sync.pub

# 阶段 2：明确激活项目专用密钥
python3 native-host/install.py --activate-dedicated-key
```

`--activate-dedicated-key` 会保留旧配置中的服务器地址和端口，并先发送一个不含 Cookie 的空请求做 SSH 认证探测。只有专用公钥已被服务器接受、且 `known_hosts` 校验通过时，才会替换本机配置；认证失败时本机旧配置和已安装文件保持不变。激活后在扩展设置页选择 `rsshub-cookie-sync` 并保存，再点击“立即同步”。

服务器 provision 会替换而不是追加同步公钥，因此新公钥写入后旧私钥会立即失效。请紧接着执行激活；若认证探测失败，应通过管理员连接重新 provision 旧公钥以恢复旧连接。

如果旧配置文件本身损坏或缺少必要字段，安装器不会猜测迁移；请先备份并按错误提示修复。新的全新安装默认使用 `~/.ssh/rsshub-cookie-sync` 和 `~/.ssh/known_hosts`。

## 卸载

正常安装后的固定入口是：

```sh
"$HOME/Library/Application Support/rsshub-cookie-sync/uninstall.sh"
```

也可以使用 macOS curl bootstrap：

```sh
curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh -s -- uninstall
```

如果是从源码目录安装，或固定入口不存在，才使用源码兼容命令：

```sh
python3 native-host/uninstall.py
```

卸载只清理本项目安装的 Host、manifest 和配置，不会删除 `~/.ssh/` 中的任何密钥、known_hosts 条目或服务器数据。服务端卸载会删除候选 Cookie 和 `/var/lib/rsshub-cookie-sync` 状态，只删除确认由本项目创建的 `rsshub-sync` 账号；如果安装前已经存在同名账号，只移除本项目添加的 SSH 授权和相关配置，不删除账号本身。服务端只保留 Compose、运行中的 RSSHub 和 live `rsshub.env`。本机卸载默认保留 `~/.ssh/rsshub-cookie-sync` 及其 `.pub`。
