# 同步模式与订阅监控（v1.3.0）

默认 `sync_mode: "direct"`：扩展自动同步、单站点重试及 `manual-update` 都只检查凭证格式，直接更新 live env，不在上传前后请求知乎、微博或 X 的登录验证接口。凭证相同不重建容器；变更时保留 Compose 检查、RSSHub 健康检查、事务和失败回滚。扩展成功日志标记“直接同步，未验证上游登录态”。

旧配置缺少该字段时也采用 direct。`sync_mode: "verified"` 可显式恢复原来的登录探针、已验证候选和自动切换策略；下面的订阅监控仅用于 direct 模式。

## 配置实际订阅路径

在服务器以 root 编辑 `/etc/rsshub-cookie-sync/config.json`，**合并**以下字段，保留原有 deployment、rsshub、Bark 等配置，权限保持 `0600`。示例账号需换成自己的订阅目标：

```json
{
  "sync_mode": "direct",
  "providers": {
    "zhihu": {"rsshub_route": "/zhihu/people/activities/example"},
    "twitter": {"rsshub_route": "/twitter/user/example"},
    "weibo": {"rsshub_route": "/weibo/user/1234567890"}
  }
}
```

只填以对应 provider 开头的路径，不填域名、查询参数或片段。服务端使用已配置的本机 `rsshub.base_url` 和 `rsshub.access_key`，不向公网发送浏览器 Cookie。检查的是同一路由在本机 RSSHub 的返回，不覆盖公网 DNS、TLS 或反向代理健康状态。默认路径为 null；未配置路径或没有 live 凭证时跳过该站点，不触发登录失效告警。Twitter 多账号池仍禁止同步器自动接管，但可检查已配置的订阅路由。

配置由每次命令重新读取，无需重启容器。立即检查：

```sh
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync monitor --json
/usr/local/lib/rsshub-cookie-sync/rsshub-cookie-sync status --json
```

状态中每个 provider 的 `route_probe`、`route_probe_at`、`route_failures`、`route_error` 分别表示路由结果、时间、连续失败次数和固定错误码。`last_probe` 保持 unknown，避免把路由成功误报为上游登录验证通过。扩展日志反映本次同步结果；定时路由结果查看服务端状态和 Bark。

## 判断与告警

每 15 分钟检查一次。HTTP 200 且正文为 RSS（含 channel）或 Atom feed 才算正常，空订阅也正常。HTTP 错误、重定向、超时、HTML、异常 XML、超过 2 MiB 的正文均记为异常；拒绝 XML DTD/实体声明。临时响应仅在内存解析，不写日志或磁盘，也不跟随重定向。

连续两次路由异常后发送 Bark，沿用通知冷却；恢复时发送恢复通知。RSSHub 服务整体不健康时仅发服务告警并跳过本轮路由检查，已有路由结果需结合时间判断是否过期。路由异常不会回滚 live 或切换旧候选。

路由失败可能来自凭证、限流、路由代码或网络；成功也可能来自 RSSHub 缓存，不能据此证明刚上传的凭证有效。不清理 Redis、不强制绕过缓存，按 RSSHub 正常缓存周期观察。真实站点可用性需要部署环境验证，模拟测试只验证上述行为。
