# 安全策略

RSSHub Cookie Sync 会处理浏览器 Cookie、Bark Device Key、SSH 私钥和服务器连接信息。请把这些内容当作登录凭证处理。

不要在 GitHub Issue、Pull Request、讨论区、截图、日志附件或公开评论中提交：

- `ZHIHU_COOKIES`、`WEIBO_COOKIES`、`TWITTER_AUTH_TOKEN` 或其他 Cookie/Token；
- Bark Device Key、完整 Bark URL、SSH 私钥；
- 未经脱敏的 `known_hosts`、请求头或完整 Compose 配置；
- 能同时暴露服务器地址、SSH 用户和连接细节的完整日志。

如果凭据已经公开，先立即注销或重新生成凭据，再清理公开内容。仅删除 Issue 或编辑帖子不能撤销已经被复制的凭据。

## 报告漏洞

请使用仓库 **Security → Report a vulnerability**，通过 GitHub Private Vulnerability Reporting 私下提交：

- 受影响的版本或提交；
- 不含真实凭据的最小复现步骤；
- 影响范围、利用条件和建议修复方式；
- 必要的脱敏日志。

不要通过公开 Issue 报告尚未修复的漏洞。如果仓库暂时没有私密报告入口，请先不要公开漏洞细节，等待维护者提供私密渠道。

## 本项目如何保护凭据

- 扩展只读取知乎和微博两个固定请求地址会携带的 Cookie；
- Cookie 不写入扩展持久化存储、命令行参数、环境变量或日志；
- Native Host 只把 Cookie 放进 SSH 标准输入，并使用服务器上的受限强制命令；
- 服务器把 live env 和候选文件放在仅 root 可读的目录中，更新使用原子写入、锁和回滚；
- Bark Device Key 只保存在服务器端权限为 `0600` 的配置文件中，并放在 HTTPS 请求体，不放入 URL；
- 私钥留在 Mac 的 `~/.ssh/`，扩展设置页只看到文件名，不会读取私钥内容；
- “复制 Cookie”是用户明确点击、确认并授权剪贴板后的应急例外。复制后 Cookie 可能受操作系统剪贴板机制保护范围之外的程序读取，因此只应粘贴到可信位置，并按系统策略清理剪贴板。

主机密钥校验使用严格模式。首次连接时应通过独立可信渠道核对服务器的 Ed25519 指纹，不要把网络上第一次得到的条目直接当作信任根，也不要关闭主机密钥校验。

## 信任边界

拥有当前 Mac 用户权限的程序可能攻击浏览器运行环境或读取该用户可读的 SSH 文件；拥有服务器 root 或 Docker 管理权限的人可能读取 RSSHub 容器的 Cookie 环境变量。这些风险无法由应用层完全消除，请保护操作系统账户、Edge 配置文件、服务器 root 账号和 SSH 私钥。

固定的扩展 ID 和 Native Host 名称只是产品标识，不是签名或凭证。请只从本仓库或 GitHub Release 获取扩展，并核对来源。

## 支持版本

安全修复优先覆盖 GitHub Releases 中最新的稳定版本和 `main` 分支。生产环境建议使用最新稳定版本，并在升级前阅读 Release Notes。
