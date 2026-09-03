# RSSHub Cookie Sync Native Messaging Host

[返回项目总览](../README.md)

这是 Microsoft Edge Manifest V3 扩展与 RSSHub 服务器之间的本机桥接程序。它只使用 Python 3.9+ 标准库，默认安装到 macOS 当前用户的 Application Support 目录，不需要第三方 Python 包。

Native Host 不负责登录，也不会保存 Cookie。它从 Edge 的 Native Messaging stdin 收到经过校验的请求，在同一进程内转发给服务器端受限 SSH 账号，然后只把脱敏状态返回给扩展。

## 安装前提

- macOS、Microsoft Edge 和 `/usr/bin/python3`；
- 服务器端已经完成 `server/install.sh`；Native Host 安装后再把它生成的 Ed25519 公钥加入 `rsshub-sync`；
- 已从服务器管理员处取得目标 SSH 主机的 Ed25519 `known_hosts` 条目，并通过独立渠道核对了指纹；
- Edge 扩展已经安装，且知道实际扩展 ID。

服务端安装、公钥接入和 Bark 配置见 [服务端文档](../server/README.md)。

## 先核对服务器主机指纹

`install.py` 不会自动执行 `ssh-keyscan`，也不会因为缺少 `known_hosts` 而自动信任网络上看到的 key。推荐由服务器管理员在服务器控制台查看 SSH 主机指纹：

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

在 Mac 上取得目标主机的 Ed25519 known_hosts 条目（示例中的主机、端口和文件名都是占位符）：

```sh
ssh-keyscan -t ed25519 -p <ssh-port> <server-host> > /tmp/rsshub-cookie-sync-known_hosts
ssh-keygen -lf /tmp/rsshub-cookie-sync-known_hosts -E sha256
```

将两个指纹通过独立可信渠道逐字比较。只有一致时，才把该文件作为 `--known-hosts-source` 传给安装器。网络抓到的条目本身不是信任依据。

安装器接受目标服务器的精确裸主机条目（默认端口）或 `[主机]:端口` 条目，拒绝通配符和不匹配的主机；当前只接受 SSH Ed25519 主机 key。非默认端口必须使用方括号格式。

## 安装

从源码目录安装：

```sh
cd /path/to/rsshub-cookie-sync/native-host
python3 install.py \
  --extension-id <32位Edge扩展ID> \
  --server-host <server-host> \
  --server-port <ssh-port> \
  --known-hosts-source /path/to/verified-known_hosts
```

`--server-host` 是必填项，不存在任何默认服务器地址。`--server-port` 默认 `22`，`--server-user` 默认 `rsshub-sync`。服务器地址、SSH 端口和账号都只写入本机权限为 `0600` 的配置文件，不会写入 Cookie。

### 代理

默认是直连。需要经由本机 SOCKS5 代理时，必须显式同时提供：

```sh
python3 install.py \
  --extension-id <32位Edge扩展ID> \
  --server-host <server-host> \
  --server-port <ssh-port> \
  --known-hosts-source /path/to/verified-known_hosts \
  --proxy-host <socks5-host> \
  --proxy-port <socks5-port>
```

显式配置 SOCKS5 后，代理连接失败会返回 `retryable_error`，不会偷偷回退到直连；只提供其中一个代理参数也会被拒绝。Native Host 会清除子进程环境中的代理变量，因此 shell 中的 `HTTP_PROXY`、`HTTPS_PROXY` 等不会改变实际连接方式。

### 密钥

默认情况下安装器会在应用专属目录生成一个无口令 Ed25519 密钥，并在安装结束时打印公钥。只把公钥交给服务器管理员，不要上传或分享 `id_ed25519` 私钥。若已经在该安装目录准备好密钥，可以使用 `--no-generate-key`；目标私钥仍必须是安装目录内、当前用户拥有且权限 `0600` 的普通文件。

安装器只写这些本机路径：

```text
~/Library/Application Support/RSSHub Cookie Sync/native_host.py
~/Library/Application Support/RSSHub Cookie Sync/config.json
~/Library/Application Support/RSSHub Cookie Sync/ssh/id_ed25519
~/Library/Application Support/RSSHub Cookie Sync/ssh/id_ed25519.pub
~/Library/Application Support/RSSHub Cookie Sync/ssh/known_hosts
~/Library/Application Support/Microsoft Edge/NativeMessagingHosts/com.jayden.rsshub_cookie_sync.json
```

目录为用户私有，配置和私钥为 `0600`。安装过程只做本地文件操作，不连接服务器、不修改 RSSHub、不执行 Docker 命令。

安装完成后，把 `ssh/id_ed25519.pub` 通过可信管理员 SSH 会话的 stdin 交给服务器上的 `/usr/local/sbin/rsshub-cookie-sync-provision-key`。服务端首版一次只保留一个采集端公钥，再次 provision 会替换旧公钥；私钥 `id_ed25519` 永远不要离开 Mac。

## 扩展 ID 与 Native Host manifest

Host 名称固定为 `com.jayden.rsshub_cookie_sync`。它是产品标识，不是密码。

当前扩展的 `manifest.json` 含有固定的公开 RSA `key`，因此源码目录和官方 Release ZIP 在未修改 manifest 的情况下会得到同一个扩展 ID。安装器的 `--extension-id` 必须与 Edge 的 `edge://extensions` 页面显示值完全一致：

```text
<32位小写 a-p 字符的扩展 ID>
```

公开 manifest key 只让 ID 保持稳定，不是扩展签名或真实性证明。`allowed_origins` 只能检查调用方 ID，不能区分来历不明但复用了同一公开 key 的解压扩展。只加载从本仓库或 GitHub Release 获取并核对过来源的文件，并保护扩展目录和当前 Mac 用户账号。

如果你删除或修改了 manifest 的 `key`、重新生成了自己的 key，扩展 ID 会改变；此时必须把新的 ID 传给 `install.py`，并重新生成 Native Host manifest 的 `allowed_origins`。不要把私钥加入公开仓库。

安装器写出的 manifest 类似：

```json
{
  "name": "com.jayden.rsshub_cookie_sync",
  "description": "RSSHub Cookie Sync Native Messaging host",
  "path": "/Users/你的用户名/Library/Application Support/RSSHub Cookie Sync/native_host.py",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://<32位小写 a-p 扩展 ID>/"
  ]
}
```

`path` 必须是安装后 `native_host.py` 的绝对路径。Edge 只允许清单中列出的扩展 origin 启动 Host；Host 会拒绝格式异常的浏览器 origin 参数，精确的身份授权边界是 manifest 中的 `allowed_origins`。

## 数据流与安全边界

扩展通过 Chromium Native Messaging 的 4 字节 little-endian 长度帧发送版本化 JSON：

```json
{
  "version": 1,
  "providers": {
    "zhihu": { "cookieHeader": "name=value; other=value" },
    "weibo": { "cookieHeader": "name=value; other=value" }
  }
}
```

Cookie header 只在 Native Host 进程内存中存在，然后作为 SSH stdin 发送。它不会出现在 SSH 参数、环境变量、配置文件、剪贴板或日志中。Host 会限制帧大小、provider 名、Cookie 名和值、控制字符和重复 JSON 字段；SSH 使用固定的 `known_hosts`、专用身份文件和显式参数，并禁用密码认证、全局 SSH 配置、Agent、PTY、转发和连接复用。

Host 不附加远程命令，服务器端的 `authorized_keys` forced command 才是唯一远程操作。服务端只返回以下五个状态：

```text
unchanged
candidate_saved
promoted
rejected_invalid
retryable_error
```

其他响应字段和错误正文都会被丢弃。一次远程事务有固定上限；网络暂时失败不会把 Cookie 当作失效处理。

## 测试

在 `native-host` 目录运行：

```sh
python3 -B -m unittest discover -s . -p 'test_*.py' -v
```

测试会 mock SSH 和 `ssh-keygen`，不会创建真实安装文件、连接服务器或发送 Cookie。仓库根目录的 `make check` 也会运行同一组测试。

## 升级与卸载

升级时使用同一套 `--server-host`、端口、代理和 `--known-hosts-source` 参数重新运行 `install.py`。它会原子替换 Host、配置和 manifest，保留已生成的专用密钥；不会把 Cookie 写入本机磁盘。扩展升级请同步更新扩展目录并在 Edge 扩展页点击“重新加载”。

卸载本机 Native Host：

```sh
cd /path/to/rsshub-cookie-sync/native-host
python3 uninstall.py
```

卸载只移除该 Host 的 manifest、复制的 Host、配置、专用 SSH 文件和空的管理目录；不会递归删除其他目录，也不会修改服务器上的 RSSHub、secret env 或 Bark 配置。若自定义路径，请显式传入 `--app-support-dir` 和 `--edge-manifest-dir`；两者必须是当前用户目录内的专用子目录，不能是用户目录本身、外部目录或包含符号链接的路径。

## 常见限制

- 这是 macOS 用户级安装流程；Linux/Windows 的 Native Messaging manifest 路径需要自行适配；
- 当前只支持 Edge Default Profile 和源码中固定的知乎/微博请求地址；
- Edge 关闭或 Mac 睡眠时不会采集 Cookie，但服务器 timer 仍会监控并告警；
- 不自动输入账号密码，也不绕过验证码或 MFA；
- 需要服务器端已经完成受限账号、公钥和 `known_hosts` 配置，Host 不会自动信任新服务器 key；
- SOCKS5 代理是可选的，但一旦显式配置就不会回退直连。
