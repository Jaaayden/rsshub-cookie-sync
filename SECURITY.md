# 安全策略

## 重要提醒

本项目会处理浏览器 Cookie、Bark Device Key、SSH 私钥和服务器连接信息。它们都是敏感凭据。

请不要在 GitHub Issue、Pull Request、讨论区、截图、录屏、日志附件或公开评论中提交以下内容：

- `ZHIHU_COOKIES`、`WEIBO_COOKIES` 或任何其他 Cookie、Token、Authorization 值；
- Bark Device Key、完整 Bark URL、SSH 私钥、`known_hosts` 中未经脱敏的内容；
- 服务器公网地址与可用于登录的用户名、端口、命令输出组合；
- 包含请求头、环境变量或密钥文件内容的完整日志。

如果凭据已经意外公开，请立即撤销或重新生成该凭据，再处理公开内容；不要仅依赖编辑或删除 Issue 来补救。

## 报告漏洞

请使用仓库 `Security` 页面中的 `Report a vulnerability`，通过 GitHub Private Vulnerability Reporting 私下提交漏洞细节。请在报告中提供：

- 受影响的版本或提交；
- 可复现的最小步骤；
- 影响范围和可能的利用条件；
- 不含任何真实凭据的脱敏日志或测试样例。

不要通过公开 Issue 报告尚未修复的安全漏洞。若仓库暂时没有私密报告入口，请先不要公开漏洞细节，等待维护者提供私密渠道。

维护者会在确认后评估影响、准备修复，并在适当情况下发布安全公告。请尽量保留可复现所需的技术信息，但不要附上真实 Cookie 或密钥。

## 支持版本

安全修复优先覆盖：

- GitHub Releases 中最新的稳定版本；
- `main` 分支的当前代码。

旧版本可能不会收到补丁。生产环境建议使用最新稳定版本，并在升级前阅读发布说明。若问题只在旧版本出现，请先升级到最新稳定版本后再确认是否仍可复现。

## 部署安全边界

请把服务端配置文件、候选 Cookie、状态目录、SSH 私钥和 `known_hosts` 保持为仅指定用户可读；不要把它们加入 Git。浏览器扩展只应通过 Native Messaging Host 向你明确配置的服务器发送 Cookie，不应把 Cookie 写入扩展持久化存储、剪贴板、命令行参数或日志。
