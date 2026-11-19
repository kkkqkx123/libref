# 网络与镜像源处理

## 可达性现状

devs 的网络不是全通环境：

- Docker Hub 的 `registry-1.docker.io` 超时，必须走 Mirror。
- GHCR 的 API 与 token 端点可达，但 blob 会 307 跳到 `pkg-containers.githubusercontent.com`，该 CDN 在 devs 上传输极慢甚至卡死。
- npm registry、PyPI、crates.io sparse index、TUNA 镜像、daocloud mirror 可达。
- 本地开发机存在 `127.0.0.1:7890` 代理直连 Debian apt 源时偶发 502；绕过代理后 apt 下载稳定。

部署环境不直接连接公网，不引入校验和或额外安全屏障；同步完整性仍按 `docs/ops/devs-sync.md` 的文件数与体积抽查。

## Docker Hub

系统 Docker 的镜像源写在 `/etc/docker/daemon.json`：

```json
{
  "registry-mirrors": ["https://docker.m.daocloud.io"]
}
```

配置后需重启 `docker.service` 才会体现在 `docker info` 中。`node:22-bookworm-slim`、`python:3.14-slim-bookworm` 与 `rust:1-bookworm` 都通过该 Mirror 拉取。

## GHCR 基础镜像

`ghcr.io/astral-sh/uv:python3.14-bookworm-slim` 在 devs 上拉取时 manifest 正常、blob 卡在 GHCR CDN。处理方式是从可联网机器导出单个镜像，再加载到 devs：

- 在可联网机器用 Podman 拉取并 `podman save` 为 tar。
- 用 scp 传到 devs。
- 在 devs 上用 `sudo docker load -i` 载入。

不要一次导出多个镜像后共用同一个 tar：实际操作中 Docker load 后多个标签会指向同一个镜像 ID，导致 `node` 镜像里没有 npm。基础镜像应逐个导出、逐个加载，或在 devs 上直接从 Docker Hub Mirror 拉取对应标签并核对 `node -v`/`npm -v`。

## apt、npm、PyPI 与 cargo

- apt：容器内直连 `deb.debian.org` 稳定；本地构建时若继承 `http_proxy`/`https_proxy`，代理偶发 502。本地构建用 `podman build --http-proxy=false` 并清除代理环境变量。
- npm：构建阶段 `npm ci` 直连 npm registry，不需要额外 Mirror。
- PyPI：`uv sync` 直连 PyPI；同步包包含 `uv.lock`，锁文件决定解析结果。
- cargo：crates.io index 可达但低速，容易被 cargo 的低速检测中断。`simvec` 阶段设置 `CARGO_HTTP_LOW_SPEED_LIMIT=0`、`CARGO_HTTP_TIMEOUT=600`、`CARGO_NET_RETRY=10`，让慢速下载继续完成。

## 故障对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `docker pull node:*` 卡在 manifest | Docker Hub 直连超时 | 检查 `/etc/docker/daemon.json` 的 Mirror 并重启 daemon |
| `docker pull ghcr.io/astral-sh/uv:*` 卡在 blob | GHCR CDN 在 devs 不可达 | 从可联网机器单镜像导出并 load |
| `npm: not found` | 多镜像 tar 导致标签串到错误镜像 | 单独拉取/加载 `node:22-bookworm-slim` 并核对 `npm -v` |
| apt 报 `502 Bad Gateway [IP: 127.0.0.1 7890]` | 本地代理不稳定 | 构建时关闭代理传递与环境变量 |
| cargo 报 `transfer too slow` | crates.io 低速触发默认阈值 | 保留 `CARGO_HTTP_*` 设置，不额外加校验步骤 |
| `Cgroups v1 not supported` | Podman 6 不兼容 cgroup v1 | 使用系统 Docker，不在该机使用 Podman 构建 |
