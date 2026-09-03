# RSSHub Cookie Sync Native Messaging Host

[返回项目总览](../README.md)

Native Host 是 Edge 扩展和 RSSHub 服务器之间的本机桥接程序。它使用 Python 3.9+ 标准库，不需要安装第三方包，也不会保存 Cookie。

## 安装

先在 Edge 中加载 RSSHub Cookie Sync 扩展，再在 Mac 终端进入仓库目录执行：

```sh
cd ~/rsshub-cookie-sync
python3 native-host/install.py
```

官方扩展使用固定的公开 ID，所以默认命令不需要 `--extension-id`，也不需要服务器地址参数。安装器只做本地文件操作：

- 把 Host 安装到当前用户的 Edge Native Messaging 目录；
- 在 `~/.ssh/rsshub-cookie-sync` 创建 Ed25519 密钥；如果该文件已存在，则复用它；
- 使用 `~/.ssh/known_hosts` 作为 SSH 主机密钥文件；
- 显示一行公钥，供服务器上的 `rsshub-sync` 账号安装。

如果你有多个已有密钥，安装完成后可以在扩展的“连接设置”中选择 `~/.ssh/` 下的其他文件名。扩展只能看到安全文件名，不能读取私钥内容。密钥必须是当前用户拥有的普通文件，权限通常应为 `0600`（可执行 `chmod 600 ~/.ssh/<密钥文件名>`），并且必须能在无人值守状态下使用；带口令且依赖人工输入的私钥不适合定时同步。

安装器不会连接服务器、读取浏览器 Cookie 或运行 Docker。

## 主机密钥校验

Host 使用严格的 SSH 主机密钥校验，不会因为第一次连接就自动信任网络返回的 key。如果 `~/.ssh/known_hosts` 已有你核对过的目标条目，可以直接使用。

全新服务器请先通过独立可信渠道确认指纹。服务器管理员在服务器控制台执行：

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

Mac 上获取公开条目并计算指纹：

```sh
ssh-keyscan -t ed25519 -p <SSH端口> <服务器地址> > /tmp/rsshub-cookie-sync-known_hosts
ssh-keygen -lf /tmp/rsshub-cookie-sync-known_hosts -E sha256
```

两个指纹完全一致后，再加入本机文件：

```sh
cat /tmp/rsshub-cookie-sync-known_hosts >> ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts
```

`ssh-keyscan` 只是获取公开条目，不是独立信任来源。不要关闭主机密钥校验，也不要把未经核对的条目发给别人使用。

## 把公钥加入服务器

完成上面的主机指纹核对后，安装器显示的是公钥，不是私钥。使用已经核对过主机身份的管理员 SSH 连接，把它通过标准输入交给服务器：

```sh
ssh -p <管理员SSH端口> root@<服务器地址> \
  /usr/local/sbin/rsshub-cookie-sync-provision-key \
  < ~/.ssh/rsshub-cookie-sync.pub
```

如果选择了其他密钥，把最后的路径换成对应的 `.pub` 文件。公钥不会出现在远程命令参数中；私钥永远不离开 Mac。

服务器端一次保留一个采集端公钥。重新执行 provision 会替换旧公钥，因此只有你确认需要时才这样做。

## 扩展设置页

打开扩展弹窗，点击“连接设置”，填写：

- 服务器地址：运行 RSSHub 的域名或 IPv4 地址；
- SSH 端口：通常是 `22`；
- SSH 密钥文件名：选择 `~/.ssh/` 下与服务器公钥对应的密钥。

连接账号由服务端安装器固定创建为 `rsshub-sync`，不需要在扩展中填写。点击“保存连接设置”后，地址、端口和所选密钥名保存到 Native Host 的本机配置文件，不进入扩展存储。Native Host 才会读取私钥并建立 SSH 连接。

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

升级 Host：拉取新版本后再次执行：

```sh
python3 native-host/install.py
```

它会保留 `~/.ssh/` 中的密钥和主机条目，不会把 Cookie 写入本机磁盘。升级扩展后，在 `edge://extensions` 点击“重新加载”。

卸载本机 Host：

```sh
python3 native-host/uninstall.py
```

卸载只删除它安装的 Host、清单和配置，不会删除 `~/.ssh/` 中的任何密钥或 `known_hosts` 条目，也不会删除服务器上的 RSSHub 或 Compose secret。确认默认密钥没有被其他用途复用后，你可以自行删除 `~/.ssh/rsshub-cookie-sync` 及其 `.pub` 文件。服务器端仍可使用最后一次 live Cookie 运行，直到你另外停用服务端监控或清理配置。

## 测试

```sh
python3 -B -m unittest discover -s native-host -p 'test_*.py' -v
```

测试不会连接真实服务器、读取浏览器 Cookie 或使用真实私钥。
