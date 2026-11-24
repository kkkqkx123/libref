# 镜像构建方案

## 结论

镜像采用四阶段构建，只把构建产物送入运行时镜像。Python 依赖、前端产物和 simvec 原生库分别在其专用阶段生成；gcc、cargo、node、npm 等工具链不进入最终镜像。当前成品约 395 MB，标签为 `libref:<HEAD 短哈希>` 与 `libref:latest`。

## 阶段划分

| 阶段 | 基础镜像 | 主要输入 | 产物 |
|---|---|---|---|
| `builder` | `ghcr.io/astral-sh/uv:python3.14-bookworm-slim` | `src/`、`vendor/`、`packages/`、`pyproject.toml`、`uv.lock` | `/app/.venv` |
| `frontend` | `node:22-bookworm-slim` | `webui/` | `/webui/dist` |
| `simvec` | `rust:1-bookworm` | `crates/` | `/usr/local/lib/libsimvec.so` |
| `runtime` | `python:3.14-slim-bookworm` | 以上三阶段产物 | 可运行镜像 |

`builder` 用 gcc 编译 tree-sitter grammar 的 C 扩展；`simvec` 用独立 Rust 阶段满足 `Cargo.lock` v4 的 toolchain 要求，并显式指定 `/app/target` 作为构建目录。`frontend` 在 Node 阶段执行 `npm ci` 与 `npm run build`。

## 运行时内容

运行时镜像只包含：

- `/app/.venv`：Python 运行依赖。使用 `uv sync --no-editable`，`libref` 与 tree-sitter grammar 以普通安装形式进入 site-packages，不依赖源码树。
- `/usr/local/lib/libsimvec.so`：由 `SIMVEC_LIB` 指向，通过 ctypes 加载。
- `/app/webui/dist`：前端产物，由 `LIBREF_WEB_SPA_DIR` 指向。

镜像不携带 `src/`、`vendor/`、`crates/simvec/bindings/`、项目元数据文件，也不携带任何编译工具链。

运行用户为 uid 1000 的 `libref`。镜像构建时预建 `/data` 与 `/home/libref/.local/share/libref` 并归属该用户，避免挂载数据卷后无法写库。`VOLUME` 仍指向家目录下的数据目录，compose 则把数据库、快照与仓库路径指向 `/data`。

## 构建上下文

根 `.dockerignore` 排除密钥文件、本地实例配置、缓存、参考仓库、测试与部署目录、本地前端依赖与产物、Rust 构建目录等。尤其不能把 `.env`、`.env.*`、`libref.toml`、`webui/dist`、`crates/simvec/target` 送进构建上下文。

同步包仍按 `docs/ops/devs-sync.md` 生成；`docs/` 不进入同步包，镜像也不依赖它。

## 构建步骤

1. 按同步文档把源码覆盖到 devs 的 `/data/kkkqkx/libref/repo/`，并确认 `BUILD_INFO.txt` 的 `sha` 与预期一致。
2. 确认系统 Docker 正常：`/usr/bin/docker`、`docker.service` active、镜像源已配置。
3. 基础镜像就绪：Docker Hub 镜像走 registry mirror；GHCR 的 uv 镜像按 `network.md` 的方式载入。
4. 在 `repo/` 目录构建，读取 `BUILD_INFO.txt` 中的 sha 作为标签，同时打 `libref:latest`。
5. 构建日志写到 `/data/kkkqkx/libref/build-<sha>.log`，不把构建输出直接回传本地。

devs 上的系统 Docker 未安装 buildx 组件，构建时显式关闭 BuildKit；这不改变产物内容。

## 验收口径

- 镜像标签与 `BUILD_INFO.txt` 的 sha 一致。
- 容器内 `libref`、`tree_sitter_groovy`、`tree_sitter_kotlin` 均从 `/app/.venv/lib/python3.14/site-packages/` 导入。
- 容器内不存在 `gcc`、`cargo`、`node`、`npm`。
- `/usr/local/lib/libsimvec.so` 与 `/app/webui/dist/index.html` 存在。
- 运行服务后 `/health` 返回 200；`GET /` 带 `Accept: text/html` 返回外壳且 `Cache-Control: no-store`；`/assets/*` 返回 200 且 `Cache-Control: public, max-age=31536000, immutable`。

开发模式只绑回环地址。冒烟时若用 `libref serve --host 0.0.0.0` 覆盖命令，会被启动校验拒绝；应使用回环监听或 enforced 模式。
