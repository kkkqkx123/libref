# devs 环境限制

## 主机与运行时

devs（SSH 别名 `devs`，`10.10.20.116`）是一台 Ubuntu 18.04 编译服务器，内核 4.15，使用 cgroup v1，32 核、32 GB 内存，数据盘空间充足。该机承担代码同步、依赖安装、测试与镜像构建。

Docker 已按系统包方式安装，属于正式环境：

| 项 | 位置 |
|---|---|
| CLI | `/usr/bin/docker` |
| daemon | `/usr/bin/dockerd`，由 systemd 的 `docker.service` 管理 |
| socket | `/var/run/docker.sock` |
| 数据目录 | `/var/lib/docker`，storage driver 为 `overlay2` |
| 用户组 | `itc` 已加入 `docker` |
| 镜像源配置 | `/etc/docker/daemon.json` |

系统 Docker 没有 buildx 组件，构建命令需显式关闭 BuildKit。不要再向用户目录安装临时 Docker 工具链；旧的 `~/.local/share/libref-build-tools/` 与 `~/.local/share/libref-docker/` 已移除。

## Podman 不可用的原因

该机也装有 Podman 6.2，但启动时直接报 `Cgroups v1 not supported`。Podman 5 起不再支持 cgroup v1，而主机内核无法在不重启且不改启动参数的情况下切到 cgroup v2。因此 devs 上的镜像构建不使用 Podman。

曾尝试用户态 Docker 作为绕行方案：IP forwarding 需要写 `/proc/sys/net/ipv4/ip_forward`，rootless 下被拒绝；改用 host 网络时 runc 无法进入网络命名空间；换 slirp4netns 又受限于系统 glibc 与依赖版本。结论是该路径不可靠，已清理，不应复用。

## 目录与文件约束

- 同步目标为 `/data/kkkqkx/libref/`，代码解压到 `repo/`，直接覆盖，不是 git 仓库；版本标识取自随包传输的 `BUILD_INFO.txt`。
- `repo/` 内 `.venv/`、`webui/node_modules/`、`crates/simvec/target/` 等现场生成物由 devs 自己维护，不参与同步。
- `docs/`、`ref/`、`.env`、`libref.toml`、前端本地生成物均不进同步包。
- 构建日志落在 `/data/kkkqkx/libref/`，命名与同步、测试日志约定一致。

## 构建相关注意事项

- 服务器可直连 npm、PyPI、crates.io 索引与 apt 源；Docker Hub 需要 Mirror，GHCR 的 blob CDN 在该机不可达，见 `network.md`。
- `repo/` 不是 git 工作树，不能在上面执行 `git rev-parse`；镜像标签从 `BUILD_INFO.txt` 读取。
- 默认开发模式拒绝非回环监听。容器冒烟要按 `image-build.md` 的验收方式处理监听地址。
- 同步包不做哈希校验，沿用文件数与解压体积抽查；部署环境不引入额外的安全屏障或校验和机制。
