# 贡献指南

感谢参与改进 RSSHub Cookie Sync。这个项目涉及认证 Cookie 和服务器更新事务，贡献时请优先保证凭据不会离开本地安全边界。

## 开始之前

本地开发至少需要：

- Python 3.9 或更高版本；
- Node.js 20 或更高版本；
- npm、GNU Make，以及用于检查扩展压缩包的 `zip`/`unzip`。

克隆仓库后，在项目根目录运行：

```bash
make check
```

该命令会执行 Python 语法检查、服务端测试、Native Messaging Host 测试和 Edge 扩展测试。也可以按模块运行 `make test-server`、`make test-native-host` 或 `make test-extension`。

## 凭据与测试数据

测试必须使用明显虚构的占位值，例如 `name=fake-cookie-for-test`，不能使用从知乎、微博或其他网站复制的真实 Cookie。测试夹具、快照、示例配置和日志中都不得出现真实 Token、Bark Device Key、SSH 私钥或完整服务器登录信息。

请特别检查：

- 测试失败时不会打印请求头、Cookie 原文或完整上游响应；
- 新增代码不会把 Cookie 写入文件、剪贴板、命令行参数、环境变量或扩展存储；
- 错误信息只包含状态码和脱敏后的分类原因；
- 文档中的地址和账号是示例值，而不是实际部署信息。

发现疑似安全漏洞时，请遵循 [安全策略](SECURITY.md)，不要开公开 Issue。

## 提交变更

建议每个 Pull Request 只解决一个主题，并在描述中说明：

- 变更目的和影响范围；
- 是否涉及扩展权限、Native Messaging 协议、SSH、systemd、Docker Compose 或密钥处理；
- 已运行的测试命令及结果；
- 对用户升级、卸载或兼容性的影响。

提交前请确认：

- `make check` 通过；
- 没有提交本地配置、密钥、Cookie、构建产物或运行时状态；
- 公开文档中的命令可以在干净环境中理解和复现；
- 若修改了协议或安全边界，测试和文档已同步更新。

## 发布

扩展压缩包由 GitHub Actions 在 CI 和版本发布时构建。普通贡献者不需要提交 `dist/` 构建产物；维护者会在发布前检查压缩包内容和权限变更。
