# Podman 独立部署

本目录提供 Podman 专用部署入口，与 `deploy/compose.yml` 的 Docker 示例互不依赖；Docker 侧配置不做 Podman 适配。镜像定义仍复用仓库根 `Dockerfile`，前端构建、`LIBREF_WEB_SPA_DIR` 与单端口暴露均保持不变。

## 使用

1. 复制 `deploy/env.prod.example` 为 `deploy/libref.env`，填入真实密钥与站点来源。
2. 在仓库根执行 `./deploy/podman/deploy.sh`，或从任意目录用绝对路径调用；脚本按自身位置推导仓库根。
3. 脚本构建镜像、创建网络与命名卷、依次启动 Qdrant 和 libref，并等待两者健康检查通过。服务只绑回环 `8001`，TLS 与对外暴露交给宿主机 nginx。

重复执行会替换同名容器，但保留命名卷。需要彻底清理时再单独执行 `podman volume rm`。

## 与 Docker 示例的差异

- **卷属主**：镜像运行时用户为 uid 1000。rootless Podman 的 UID 映射会让普通命名卷在容器内表现为 root 所有，因此脚本对两个卷使用 Podman 专有的 `:U` 修正属主；该选项不写入 Docker 配置。
- **服务发现**：`LIBREF_QDRANT_URL` 依赖 Podman 网络内的容器名解析，需要 netavark（或旧版 CNI 的 dnsname 插件）。网络由脚本显式创建，不依赖 compose 提供器。
- **启动顺序**：脚本等待 Qdrant 健康后再启动 libref；`podman-compose` 不是前置条件。
- **重启语义**：`--restart unless-stopped` 只在当前用户会话内生效。主机重启后自动拉起需另配 systemd 用户单元或 quadlet，脚本不负责这一步。
- **路径基准**：Docker Compose 的相对路径以 compose 文件目录为基准；脚本从自身位置推导仓库根，避免 `build: .` 指错目录。

## 可调变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `LIBREF_ENV_FILE` | `deploy/libref.env` | 密钥与站点来源的环境文件 |
| `LIBREF_IMAGE` | `libref:latest` | 本地镜像标签 |
| `LIBREF_PODMAN_NETWORK` | `libref` | 专用网络名 |
| `LIBREF_DATA_VOLUME` | `libref-data` | 数据库、快照、仓库卷 |
| `LIBREF_QDRANT_VOLUME` | `qdrant-data` | Qdrant 索引卷 |
| `LIBREF_QDRANT_IMAGE` | `qdrant/qdrant:v1.20.1` | Qdrant 镜像 |
| `LIBREF_BIND_HOST` | `127.0.0.1` | 宿主机绑定地址 |
| `LIBREF_HOST_PORT` | `8001` | 宿主机端口 |
| `QDRANT_API_KEY` | 空 | Qdrant 内部 API Key，需与服务端 `LIBREF_QDRANT_API_KEY` 一致 |
| `LIBREF_DEPLOYMENT_ID` | `libref-podman` | 共享向量集合的部署身份 |

## 前端与反向代理

镜像已内置 `webui/dist`，Python 服务直接托管外壳、`/assets` 与深链回退，nginx 只需全量转发。需要 nginx 托管静态资产时沿用 `deploy/nginx.static.conf.example`，把构建产物部署到 nginx 静态目录，并确保后端持有同一份产物。

若 nginx 也放进 Podman 容器并绑定证书目录，SELinux 主机上给绑定挂载补 `:Z`；当前脚本未包含 nginx 容器，证书仍在宿主机管理。

## 运维

- 查看日志：`podman logs -f libref`。
- 健康检查：`podman healthcheck run libref`、`podman healthcheck run qdrant`。
- 停止服务：`podman stop libref qdrant`；删除容器：`podman rm -f libref qdrant`。
- 不要在同一台机器上同时运行 Docker compose 栈与本脚本，避免同名容器、网络或卷互相覆盖。
