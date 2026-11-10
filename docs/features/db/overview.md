# 数据库总览

SQLite 是唯一真源，存放全部业务数据。向量库只存可重建的派生索引，
正文一律回 SQLite 取。快照与仓库工作区落文件系统，不进库。

命令行、双出口、装配层与问答编排零 SQL，
只调服务层；SQL 集中在存储层、服务实现、数据库层与抓取解析下层。

## 连接

连接由数据库层统一打开，固定预设如下：

| 预设 | 取值 |
|---|---|
| 日志模式 | 预写日志 |
| 外键约束 | 开启 |
| 忙等待 | 5000 毫秒 |
| 同步 | 普通 |
| 临时表 | 内存 |
| 缓存 | 64MB |

调用方负责打开后执行迁移。写操作经每连接可重入写锁串行化，
锁只包同步语句批，不跨等待与休眠。

## 迁移与对象对照

迁移按版本号顺序单事务执行，成功才登记版本，重放幂等，
失败整体回滚且不登记。

| 版本 | 名 | 新增对象 |
|---|---|---|
| 0 | initial-schema | 库、版本、注册表缓存、页面、文档块、全文虚表、三触发器、四索引 |
| 1 | mining-review-schema | 接口实体、调用实例、序列模式、审核状态、两索引 |
| 2 | vector-meta | 向量元数据键值表 |
| 3 | pages-content-hash | 页面内容指纹列 |
| 4 | repos-astcache-call-scope | 仓库登记、解析缓存、调用归属三列、两索引 |
| 5 | ast-chunk-relation | 实体签名系七列、调用拆分系六列 |
| 6 | mining-provenance-index | 调用提交标识列、两索引 |
| 7 | feature-tree-cluster | 特征树、聚类、两索引 |
| 8 | governance-tables | 任务、机器人缓存、删除日志、版本锁 |
| 9 | vec-version-meta | 按版本向量记录表 |
| 10 | pattern-labels | 语义标签表与索引 |
| 11 | query-support | 地址、分组、分路、待审、任务恢复六索引 |
| 12 | call-scope | 调用作用域列、回填、作用域索引 |

## 通用约定

JSON 字段以文本存放，含文档元数据、实参、声明参数、基类、
任务参数与结果、删除明细。跨版本清理一律按版本标识过滤，
集合差清理先读存活集合再分批删除，空集合即整版清空。

整版清除固定顺序如下：

```sql
DELETE FROM seq_patterns WHERE version_id=?;
DELETE FROM feature_nodes WHERE version_id=?;
DELETE FROM api_clusters WHERE version_id=?;
DELETE FROM review_status WHERE entity_type='call_instance'
  AND entity_id IN (SELECT id FROM call_instances WHERE version_id=?);
DELETE FROM call_instances WHERE version_id=?;
DELETE FROM api_entities WHERE version_id=?;
DELETE FROM pages WHERE version_id=?;      -- 级联删除文档块与全文行
DELETE FROM versions WHERE id=?;
DELETE FROM ast_cache WHERE repo=?;
DELETE FROM repos WHERE name=?;
```

向量点在真源提交前后各清一次，崩溃窗口由集合差对账修复。
