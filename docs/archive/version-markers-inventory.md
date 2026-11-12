# 代码内版本标记汇总

时间：2026-11-12（UTC）。对象：代码内全部版本标记。结论：仅一处偏离 1，已恢复为 1；其余保持不变。

## 一、判定口径

代码内版本标记指由本仓库代码定义、用于失效旧缓存或旧数据的内部版本号。不属于此列的不动：第三方接口路径版本、领域数据中的库版本号、包标识。

* 第三方接口路径版本（外部协议，不得改）：nuget 注册地址中的 v3、npm 搜索地址中的 v1、crates 地址中的 v1、硅基流动基址中的 v1、GitLab 探测地址中的 v4。
* 领域数据（库的版本号，非代码版本）：versions 表的 version 字段、各表 version_id 外键、version_locks 版本互斥键、快照目录的版本分段。
* 包标识（初始值，非内部标记）：pyproject 的 0.1.0、抓取请求头的 libref-crawler/0.1。
* 误命中：resolution.py 中的 v8 是 Node 内建模块名。

## 二、清单与处置

* 解析器缓存版本：astminer/config.py 中 LanguageConfig 默认值及九个语言显式值，改前为 2，已全部恢复为 1。该值写入 ast_cache 主键与语法指纹，旧值为 2 的缓存行在下次索引清扫时自然淘汰。
* 查询文本版本：astminer/queries 下 QUERY_VERSION，本就是 1，不变，参与查询指纹。
* 归纳提示版本：config.py 中 induction_prompt_version，本就是 v1，不变；落库字段为 schema.py 中 pattern_labels.prompt_version 默认值 v1，同为联合唯一键一部分，不变。
* 数据库迁移序列：db/migrations.py 中 Migration 序号 0 至 19 及 schema_migrations 记录表，不变。迁移序号是建库顺序标识，重编号或合并会破坏已建库，不视为可回退的版本标记。
* 向量记录：vec_version_meta 与 schema_meta 中的模型名加维度，非数字版本，不变；维度变化才重建集合，模型名变化按版本懒重嵌。

## 三、后续规则

开发阶段不再上调上述任何标记，统一保持为 1。语义变化由现有指纹与重建逻辑覆盖，语法指纹已包含规则文本摘要。
