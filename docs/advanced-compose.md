# Compose 高级说明

普通安装请使用项目首页的一键命令。这里仅说明非标准布局、多实例服务器和排障时可能用到的参数。

## 这些名称分别是什么

- Compose 文件：`docker-compose.yml` 或 `compose.yml`，描述整套 RSSHub 服务；
- project：Compose 为一套服务使用的逻辑项目名；
- service：Compose 文件中 `services:` 下面的键，例如 `rsshub`、`redis`、`browserless`；
- 容器名：实际运行中的容器名称。它不是这里要求填写的 service 名。

因此，普通用户不需要先查询容器 label 或手工推断名称。安装器会检查 Compose 文件，并默认使用 service `rsshub`。

## `config --services` 是什么

如果安装器提示你选择 service，可以在服务器上运行：

```sh
docker compose -f /绝对路径/docker-compose.yml config --services
```

它只会把 Compose 文件中定义的 service 键一行一个打印出来，例如：

```text
rsshub
redis
browserless
```

这不是启动命令，也不是容器状态查询；它只是帮助确认“哪一个键代表 RSSHub”。通常选 `rsshub`。不需要把这条命令的输出粘贴到公开 Issue，因为它可能暴露你的部署结构。

## project 名称为什么通常不用填

不带 `--project-name` 时，安装器会让 Docker Compose 自己解析 project：优先尊重 Compose 文件的顶层 `name:` 和 Compose 规则，再使用文件所在目录等默认值。这样可以避免手工填写错误名称。

只有在同一服务器有多套 Compose，或者你必须与已有脚本保持特定 project 名称时，才显式指定：

```sh
sudo /path/to/rsshub-cookie-sync/server/install.sh \
  --compose-file /srv/rsshub/docker-compose.yml \
  --project-name my-rsshub \
  --service-name rsshub \
  --rsshub-base-url http://127.0.0.1:1200
```

`--project-name` 不会创建新的 Docker project，也不会迁移其他目录；它只告诉同步器使用哪一个 Compose project 操作目标 service。

## service 名称和健康地址

官方 RSSHub Compose 通常使用 `rsshub`，所以这是默认值。如果你的 YAML 使用 `app` 或其他名称，交互式安装器会列出服务并让你选择；非交互安装可以显式填写：

```sh
--service-name app
```

RSSHub 健康地址默认是：

```text
http://127.0.0.1:1200/healthz
```

只有你把 RSSHub 映射到服务器其他本机端口时才需要：

```sh
--rsshub-base-url http://127.0.0.1:1300
```

服务端只接受本机回环地址，避免同步器变成公网请求转发器。不要填写公网地址或带 Cookie 的 URL。

## Compose 迁移范围

安装器只修改指定 service 的三项环境变量，并加入相对 Compose 目录的 raw env_file：

```yaml
env_file:
  - path: ./secrets/rsshub.env
    format: raw
```

它不会处理其他 service，也不会改变网络、卷、端口、镜像或普通环境变量。每次候选提升时只重建指定 RSSHub service，不会执行 `down`、拉取镜像或清理 Docker 数据。

## 目标已改变时

已安装的实例会保存 Compose 路径、project、service 和健康地址。普通重装如果这些值不同会拒绝继续，防止路径写错。确实要切换目标时，先确认新文件和新服务，再显式使用：

```sh
sudo /path/to/rsshub-cookie-sync/server/install.sh \
  --compose-file /srv/another-rsshub/docker-compose.yml \
  --project-name another-rsshub \
  --service-name rsshub \
  --rsshub-base-url http://127.0.0.1:1200 \
  --replace-deployment
```

这个选项不会删除旧 Compose、旧容器或旧 secrets。切换前请按自己的备份策略保存旧部署；如果只是升级同步器，请不要使用它。

## 安全限制

安装器要求源码、Compose 文件及其父目录由 root 管理，拒绝符号链接和 group/world 可写路径。Compose 校验使用 `config --quiet`，只验证配置而不把展开后的环境变量打印到终端。

如果 `config --quiet` 失败，先确认 Docker Compose 版本至少为 v2.30，并检查 YAML 语法和 service 名称。不要运行不带 `--quiet` 的 `config` 后把输出发到网上，因为其中可能包含环境变量。
