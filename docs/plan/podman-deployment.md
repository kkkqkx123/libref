# Podman 独立部署方案

> 范围：在现有 Docker 示例之外提供一套 Podman 专用部署入口。本文只写结论、边界与实施顺序，不贴完整脚本。

## 结论

不为 Podman 改造 `Dockerfile` 与 `deploy/compose.yml`。两者职责不同：Docker 示例继续服务 Docker Compose 用户，Podman 使用独立脚本，直接调用 Podman 原生命令完成构建、网络、卷与容器编排，不依赖 `podman-compose` 提供器。

镜像定义仍复用根 `Dockerfile`：它是 OCI 构建描述，Podman 可直接构建，其中前端构建阶段、`LIBREF_WEB_SPA_DIR` 与单端口暴露保持不变。

## 为什么要独立

- **UID 映射**：镜像运行时用户为 uid 1000。rootless Podman 下容器 root 映射到宿主机普通用户，uid 1000 映射到从属 UID；普通命名卷在容器内表现为 root 所有，非 root 进程不可写。Podman 专用卷挂载使用 `:U` 修正属主，该语义不应混入 Docker 示例。
- **服务发现**：`LIBREF_QDRANT_URL=http://qdrant:6333` 依赖 Podman 网络内的容器名解析，需要 netavark 或旧版 CNI 的 dnsname 插件；该假设属于 Podman 运行时前提。
- **重启语义**：`--restart unless-stopped` 只在用户会话存在期间有效；跨主机重启需要 systemd 用户单元或 quadlet，不作为脚本职责。
- **路径基准**：Podman  compose 的相对路径以 compose 文件目录为基准；独立脚本从自身位置推导仓库根，避免 `build: .` 指向错误目录。

## 部署内容

脚本按固定顺序创建隔离网络与两个命名卷，构建本地镜像，先启动 Qdrant 并等待其健康检查通过，再启动 libref 服务。libref 继续只绑回环 `8001`，TLS 与前端静态托管维持既有约定：镜像内置 `webui/dist`，Python 服务处理外壳与深链回退，nginx 示例可继续放在宿主机或独立容器中。

环境变量仍来自 `deploy/libref.env`，密钥不进镜像；数据卷只存数据库、快照、仓库与 Qdrant 索引，前端属于镜像层。脚本重复执行会替换同名容器，但不删除卷，避免误删数据。

## 实施顺序

1. 编写本设计文档。
2. 新增 `deploy/podman/deploy.sh`，用 Podman 原生命令实现上述编排。
3. 新增 `deploy/podman/README.md`，说明 rootless 前提、卷属主、网络解析、重启限制与 nginx 静态复用方式。
4. 在 `deploy/README.md` 与 `docs/plan/frontend-deployment.md` 增加入口指引，不改动 Docker 侧配置。
5. 验证：脚本语法检查、参数展开与幂等路径检查；有 Podman 环境时再执行真实构建与启动。
