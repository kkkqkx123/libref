# 注册表解析实际验证记录

> 对象：`docs/plan/long-term/registry-resolver.md` 的实现（`crawler/registry.py`、`services/resolver.py` 及配套改造）。
> 时间：2026-11-08（UTC）。方式：真实网络直连 `pypi.org` 与 `registry.npmjs.org`，加离线单元测试。

## 1. 验证方法

分三层，由轻到重：

* 离线单元验证（`tests/test_registry.py`，18 项）：`MockTransport` 伪造两生态应答，断言解析、排序、覆盖优先、缓存命中零网络、过期降级、离线语义、三分类错误。联网无关，进常态门禁。
* 真实注册表冒烟：先调快照查询看原始元数据形态，再调名称解析走完整优先级链（含覆盖、缓存、版本确认），覆盖双生态热门包、不存在包、指定版本、作用域包、裸名。
* 文档地址可达性：解析出的文档地址逐个真实 GET，确认不是死链。

## 2. 验证结果

### 2.1 热门包解析（真实网络）

| 包 | 解析出文档地址 | 版本 | 文档站 GET |
|---|---|---|---|
| pypi:requests | `https://requests.readthedocs.io` | 2.34.2 | 200 |
| pypi:numpy | `https://numpy.org/doc/` | 2.5.4 | 200 |
| pypi:httpx | `https://www.python-httpx.org` | 0.28.1 | 200 |
| pypi:pydantic | `https://pydantic.dev/docs/validation/latest/get-started/` | 2.14.3 | 200 |
| npm:express | `https://expressjs.com/` | 5.3.0 | 200 |
| npm:react | `https://react.dev/` | 19.3.1 | 200 |
| npm:lodash | `https://lodash.com/` | 4.18.1 | 200 |
| npm:typescript | `https://www.typescriptlang.org/` | 7.0.2 | 200 |
| npm:@babel/core（生态推断） | `https://babel.dev/docs/en/next/babel-core` | 8.1.0 | 200 |

### 2.2 边界语义（真实网络）

* 双生态不存在包均返回未收录结论，不再尝试抓取。
* 指定已发版 `2.28.0` 确认通过；指定私有版本 `9.9.9-custom` 仍放行但标记未经确认。
* 裸名无生态正确拒绝，要求补生态参数；`@babel/core` 无生态输入正确推断为 `npm`。

### 2.3 全量回归

`77 passed, 5 skipped`（跳过项为需外部向量服务与 MCP 二进制的用例）。

## 3. 验证中发现并已修复的问题

1. 仓库地址混入文档候选：`httpx` 的主页（github 仓库）曾成为第二候选，`pydantic` 的仓库提示曾误取赞助链接。修复为候选一律排除仓库托管域名，仓库提示优先源码类条目并跳过赞助类条目。复验 `httpx` 候选只剩官网，`pydantic` 仓库提示为真实源码仓。
2. 无稳定版兜底取列表末项不可靠：实测 `react` 的版本字典末项是预发布实验版本，`PyPI` 字典序下旧版排在新版之后。修复为过滤预发布后缀后按版本号比较取最大，实测两类形态均取到正确最大稳定版，设计文档已同步更新。
3. 作用域包初次探查报错经复核为探查脚本写法问题（绕过解析入口直调快照），推断链本身正常，端到端复验通过。

## 4. 结论

两生态名称解析、版本列举、文档地址排序、缓存与离线、覆盖机制、出口透传均按设计工作，解析出的文档地址全部真实可达。验证通过。
