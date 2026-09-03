# SSH 高级说明

普通用户只需运行 `python3 native-host/install.py`，然后在扩展“连接设置”填写服务器地址、端口并选择密钥。本页说明首次主机密钥核对、自定义扩展和旧部署迁移。

## 默认文件

Native Host 默认使用当前 Mac 用户的：

```text
~/.ssh/rsshub-cookie-sync
~/.ssh/rsshub-cookie-sync.pub
~/.ssh/known_hosts
```

这不是应用私有的密钥目录。已有 `~/.ssh/` 密钥可以在扩展设置页选择；对应的公钥必须已经安装在服务器 `rsshub-sync` 账号中。私钥不会进入浏览器扩展，也不会上传到服务器。自动任务不能弹出密码输入，因此复用的私钥必须能在无人值守状态下使用。

如需在安装时指定已有密钥，可使用本地兼容参数：

```sh
python3 native-host/install.py --identity-file ~/.ssh/<已有密钥文件名>
```

密钥必须是 `~/.ssh/` 下的普通文件，由当前用户拥有，并且权限通常为 `0600` 或更严格。需要时执行 `chmod 600 ~/.ssh/<已有密钥文件名>`。普通用户不需要这个参数，安装后在扩展设置选择即可。

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

`ssh-keyscan` 只负责获取公开条目，不能代替指纹核对。非 22 端口的条目必须使用 `[主机]:端口` 格式。不要使用通配符、哈希 host 条目或关闭主机密钥校验。

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

旧版本可能把私钥和主机密钥放在 Application Support。重新运行 `python3 native-host/install.py` 会尽量保留可验证的旧配置；若旧配置曾写入其他 SSH 用户名，安装器会安全迁移为固定的 `rsshub-sync`，该旧用户名绝不会进入新的 SSH 命令。新的安装默认使用 `~/.ssh/rsshub-cookie-sync` 和 `~/.ssh/known_hosts`。完成迁移后，应在扩展设置页确认服务器地址和 Ed25519 密钥选择，并重新核对主机指纹、授权对应公钥。

## 卸载

```sh
python3 native-host/uninstall.py
```

卸载只清理本项目安装的 Host、manifest 和配置，不会删除 `~/.ssh/` 中的任何密钥、known_hosts 条目或服务器数据。确认默认密钥未被其他用途复用后，可自行删除 `~/.ssh/rsshub-cookie-sync` 及其 `.pub` 文件。
