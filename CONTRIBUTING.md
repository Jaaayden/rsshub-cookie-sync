# 贡献指南

感谢参与改进 RSSHub Cookie Sync。这个项目涉及认证 Cookie 和服务器更新事务，贡献时请优先保证凭据不会离开本地安全边界。

## 开始之前

本地开发至少需要：

- Python 3.9 或更高版本；
- Node.js 20 或更高版本；
- npm、GNU Make，以及用于检查扩展压缩包的 `zip`/`unzip`。

克隆仓库后，先安装一次 lint 工具，再在项目根目录运行检查：

```bash
python3 -m pip install -r requirements-dev.txt
npm ci --prefix extension
make check
```

`make check` 会执行 Python 语法检查、lint（ruff 和 eslint）、服务端测试、Native Messaging Host 测试、Edge 扩展测试，以及 Native Host 与服务端校验规则的一致性测试。也可以按模块运行 `make test-server`、`make test-native-host`、`make test-extension`、`make test-contract` 或 `make lint`。

## 代码结构

- `extension/`：Edge 扩展（Manifest V3，无构建步骤）。
- `native-host/`：Mac 上的 Native Messaging Host 与安装器。
- `server/rsshub_cookie_sync.py`：服务端入口脚本；实现位于 `server/rsshub_sync/` 包，按依赖从底层到上层分为常量/错误、文件与校验、配置、外部系统（HTTP、Docker、Bark）、Compose 迁移、状态机和命令行。包内部使用相对导入，只依赖标准库，安装时整个目录与入口脚本一起复制。
- Native Host、服务端和扩展各自带一份 Cookie 校验规则，因为它们分别部署、无法共享代码。`tests/test_protocol_contract.py` 保证 Native Host 放行的内容服务端不会拒绝；修改任意一处校验时请同步检查它。

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
