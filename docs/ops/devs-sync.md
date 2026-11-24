# devs 服务器同步与远程校验说明

本地全量校验耗时高；devs（`ssh devs`）持有暖 uv 依赖缓存、32 核算力，且是大规
模测试的执行机。代码同步到 devs 跑校验，本说明约定同步方式。devs 侧
Python/uv/解释器/cc 包装等环境已按项目约定一次性配置完毕（requirements 见
`AGENTS.md`），下文只约定同步与校验命令，不重复环境配置要求。

## 目标位置

- 同步包存放：`/data/kkkqkx/libref/`
- 代码解压位置：`/data/kkkqkx/libref/repo/`（直接覆盖，非 git 仓库；`.venv` 在其中，同步不触碰）
- 校验日志：`/data/kkkqkx/libref/` 下按 `test-<主题>.log` / `check-<主题>.log` 命名
- 本地包暂存：`~/sync-pkgs/`（与 linkrs 同目录惯例）

## 传输方式选择

| 变更规模 | 内容 | 方式 |
|---|---|---|
| 小规模（数个文件） | 改动的源文件 | `scp` 直接覆盖远端对应路径 |
| 大规模（多文件/重构/首次） | 整个工作树 | `tar.gz` 打包 + `scp` + 远端覆盖解压 |
| 任何情况 | `.venv/`、`webui/node_modules`、`crates/simvec/target`、构建产物 | 永不传输 |

构建产物永不传输：devs 靠自身暖缓存增量编译；本地 venv 与工具链绑定，传过去也
无法复用。打包格式用 `.tar.gz` 而非 `.tar.zst`：devs 的 tar 1.29 不支持
`--zstd`，源码包体积下 gzip 足够。

不使用校验和：同步包不用哈希校验，改用包内文件数与解压后体积抽查。

## 打包排除清单

以仓库根为基准，打包时排除：`*/.git`（含 submodule 的 gitlink 文件）、
`.venv/`、`ref/`（只读外部参考仓库）、`.env`/`.env.*`、本地实例配置
`libref.toml`、`.atomcode/`、`webui/node_modules/`、`webui/dist/`、
`crates/simvec/target/`、`data/`、`logs/`、
`tests/e2e/repos|outputs|snapshots/`、`tests/e2e/bench/fixtures|data|outputs/`、
各级 `node_modules/`、`__pycache__/`、`.mypy_cache/`、`.ruff_cache/`、
`.pytest_cache/`、各级 `build/`、`*.egg-info/`、`*.so`（均为本地编译中间产物）。

`uv.lock` 必须进包：devs 的 `uv sync` 以锁文件为准，保证与本地解析一致。
`vendor/tree-sitter-groovy|kotlin` 源码必须进包（uv workspace editable 成员），
但其编译中间产物（`build/`、`*.so`）不进包，由 devs 现场重编。

## 同步步骤

本地打包（包名以 HEAD 短哈希标记）：

```bash
cd /home/kkkqkx/code
sha=$(git -C libref rev-parse --short HEAD)
tar -czf ~/sync-pkgs/libref-src-$sha.tar.gz \
  --exclude='*/.git' --exclude='libref/.venv' --exclude='libref/ref' \
  --exclude='libref/.env' --exclude='libref/.env.*' --exclude='libref/libref.toml' \
  --exclude='libref/.atomcode' \
  --exclude='libref/webui/node_modules' --exclude='libref/webui/dist' \
  --exclude='libref/crates/simvec/target' \
  --exclude='libref/tests/e2e/repos' --exclude='libref/tests/e2e/outputs' \
  --exclude='libref/tests/e2e/snapshots' \
  --exclude='libref/tests/e2e/bench/fixtures' \
  --exclude='libref/tests/e2e/bench/data' \
  --exclude='libref/tests/e2e/bench/outputs' \
  --exclude='libref/data' --exclude='libref/logs' \
  --exclude='*/node_modules' \
  --exclude='*/__pycache__' --exclude='*/.mypy_cache' \
  --exclude='*/.ruff_cache' --exclude='*/.pytest_cache' \
  --exclude='*/build' --exclude='*.egg-info' --exclude='*.so' \
  libref
```

记录构建信息（HEAD 哈希、日期、本地 uv/python、未提交变更），随包传输：

```bash
{ echo "sha=$sha"; echo "date=$(date -u +%FT%TZ)"; uv --version | sed 's/^/local_uv=/';
  (cd libref && uv run python -V 2>/dev/null | sed 's/^/local_python=/');
  git -C libref status --short; } > ~/sync-pkgs/libref-src-$sha.BUILD_INFO.txt
scp ~/sync-pkgs/libref-src-$sha.tar.gz ~/sync-pkgs/libref-src-$sha.BUILD_INFO.txt \
  devs:/data/kkkqkx/libref/
```

远端覆盖解压并抽查：

```bash
ssh devs "cd /data/kkkqkx/libref && tar xzf libref-src-$sha.tar.gz -C repo --strip-components=1 \
  && cp libref-src-$sha.BUILD_INFO.txt repo/BUILD_INFO.txt \
  && tar tzf libref-src-$sha.tar.gz | wc -l && du -sh repo"
```

抽查口径：scp 前后包内文件数一致、`repo/` 体积量级符合预期即视为完整。远端跑过
校验后 `repo/` 内会多出 `.venv/`、`__pycache__/`、`vendor/*/build/` 等现场生成物
（约 350MB），不计入比对。

小规模变更直接传文件，路径与仓库根一一对应：

```bash
scp src/libref/services/docs/answer.py devs:/data/kkkqkx/libref/repo/src/libref/services/docs/answer.py
```

## 远端校验

devs 上 uv 不在默认 PATH，先导出。耗时校验放后台跑，日志落盘到 `/data/kkkqkx/libref/`：

```bash
ssh devs "export PATH=\$HOME/.local/bin:\$PATH && cd /data/kkkqkx/libref/repo \
  && nohup uv run pytest -q > ../test-full.log 2>&1 &"
```

常用验证命令（均在 `repo/` 内、导出 PATH 后执行）：

```bash
uv run ruff check src tests librefctl/src
uv run mypy src librefctl/src
uv run pytest -m unit -q
uv run pytest -m integration -q
uv run pytest -q          # 全量；e2e 默认跳过，requires_qdrant / requires_simvec 需现场自备服务或产物
```

## 镜像构建

镜像分层、devs 运行时约束与网络处理的完整说明见 `docs/dev/`。

devs 的 Docker 是系统包安装：`/usr/bin/docker` 与 `/usr/bin/dockerd`，daemon 由 systemd 管理，socket 为 `/var/run/docker.sock`，数据目录为 `/var/lib/docker`，`itc` 已加入 `docker` 组。镜像源写在 `/etc/docker/daemon.json`，当前使用 `https://docker.m.daocloud.io`。不再使用用户目录下的临时 Docker 工具链。

构建前按上文同步源码。GHCR 基础镜像在该机拉取 blob 会卡在 GHCR CDN，可先从可联网机器导出后用 `sudo docker load -i` 载入；Docker Hub 基础镜像走镜像源直接拉取。构建命令（当前镜像无 buildx 组件，显式关闭 BuildKit）：

```bash
cd /data/kkkqkx/libref/repo
sha=$(sed -n 's/^sha=//p' BUILD_INFO.txt)
sudo env DOCKER_BUILDKIT=0 docker build -f Dockerfile -t libref:$sha -t libref:latest .
```

镜像为四阶段构建：Python 依赖、前端产物、simvec 原生库分别构建，运行时镜像只含 `.venv`、`libsimvec.so` 与前端 `dist`，不含 gcc、cargo、node 工具链。构建后用 `docker run --rm libref:$sha` 核对入口与导入路径。

## 反向取回

日志和结果体积小，直接取回：

```bash
mkdir -p logs && scp devs:/data/kkkqkx/libref/test-full.log ./logs/
```

## 注意事项

- 覆盖解压不删除远端多余文件：改名或删除源文件后，需手动删除远端对应文件，
  否则旧文件仍会被收集进测试。基准性结论建议先 `rm -rf repo` 再全新解压复核
  （之后需重跑 `uv sync` 重建 `.venv`）。
- submodule（`crates/simvec`、`vendor/tree-sitter-groovy`、
  `vendor/tree-sitter-kotlin`）以包内 checkout 为准：日常以全量包同步，
  单个文件的快速 scp 路径与仓库根一一对应。
- `vendor/tree-sitter-*` 以 editable 安装且含 C 编译产物：更新其语法源后，
  远端必须重跑 `uv sync` 触发本地重编，否则运行时加载的仍是旧 grammar。
- webui 前端：devs 无 node/npm，不在本流程编译；只同步源码，构建留在本地。
- `.env`、本地 `libref.toml`、`data/`、`ref/` 永不进包；secrets 仅经环境变量
  传递（见 `AGENTS.md`）。
- 当前全量包约 5MB，单传即可；若显著增大再考虑非高峰时段传输或
  `rsync --partial --append-verify` 断点续传。
