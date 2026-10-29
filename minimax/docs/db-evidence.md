# 数据库使用记录（seekdb / OceanBase）

## 1. 连接与版本

```
backend : seekdb（MySQL 协议）
label   : seekdb/MySQL 5.7.25-OceanBase seekdb-v1.4.0.0
database: sddctx（不存在时由 sddctx 自动 CREATE DATABASE）
driver  : pymysql 1.2.3（纯 Python）
会话参数: ob_query_timeout = 60000000、ob_trx_timeout = 60000000
```

`python -m sddctx doctor` 的输出见 [`logs/01_doctor.log`](logs/01_doctor.log)。

## 2. 表结构与落库规模

| 表 | 行数 | 作用 |
| --- | --- | --- |
| `pc_documents` | 16 | 文档级元数据：类别、路径、标题、规格版本、内容 sha256 |
| `pc_chunks` | 76 | 块级正文：symbol、行号区间、内容、块长（BM25 的 dl） |
| `pc_postings` | 3698 | 倒排表：`(term, chunk_id, tf)` |
| `pc_df` | 1382 | 文档频率：`(term, df)`，BM25 的 idf 来源 |
| `pc_runs` | 2+ | 每次上下文包的完整 JSON（可回溯「当时用的是哪一版规格」） |
| `pc_state` | 3+ | 最近一次导入报告、每次 verify 的摘要 |

16 个文档 = 4 个规格 + 6 个代码模块 + 6 个测试文件；76 个块 =
规格 6×4=24 + 代码符号块 + 测试用例块（索引的是「修改前」的 `refund.py`）。
全量导入日志见 [`logs/04_ingest_v1.log`](logs/04_ingest_v1.log)：

```
导入完成（seekdb）：扫描 16 个文档（重建 16 / 跳过 0），本次生成 76 个块 / 3685 条倒排
库内现有：76 个块 / 3685 条倒排 / 1378 个检索词
规格版本：v1   耗时：67807 ms
SQL 抽样：INSERT INTO pc_postings ...（3685 行）、INSERT INTO pc_df SELECT term, COUNT(*) FROM pc_postings GROUP BY term
```

增量：`content_sha` 未变的文档整篇跳过，只重建变更文档的块与倒排；
规格 v1 → v1.1 的切换只重建 1 个文档 6 个块（3.3 秒），切回 v1 再重建 6 个块（5.3 秒）：
[`logs/15_ingest_v2.log`](logs/15_ingest_v2.log)、[`logs/18_ingest_v1_back.log`](logs/18_ingest_v1_back.log)。
打完补丁后重导 `refund.py` 同样只重建 1 个文档 7 个块，耗时 3.0 秒
（[`logs/13_ingest_after_patch.log`](logs/13_ingest_after_patch.log)）。

## 3. 检索 SQL（每次组装上下文执行 4 条）

以 `run-20261029T211351-TASK-0007`（规格 v1）为例，语料统计 `chunks=76, avgdl=80.32`：

```sql
-- ① 文档频率：BM25 的 idf
SELECT term, df FROM pc_df WHERE term IN (?, ?, … 96 个检索词)
-- 96 个检索词里有 69 个在语料中出现（每类各查一次，行数相同）

-- ② 候选块与词频：按类别过滤，一次取回 tf / 块长 / 符号 / 路径
SELECT p.chunk_id, p.term, p.tf, c.token_len, c.symbol, c.path
FROM pc_postings p JOIN pc_chunks c ON c.chunk_id = p.chunk_id
WHERE p.term IN (?, ?, …) AND c.category = ?
-- spec 159 行 / code 170 行 / test 357 行

-- ③ 取 Top-K 正文
SELECT chunk_id, doc_id, category, path, symbol, heading, start_line, end_line, content
FROM pc_chunks WHERE chunk_id IN (?, …)
-- spec 12 行 / code 15 行 / test 21 行（每类取 3×limit 再裁剪）

-- ④ 取来源的规格版本
SELECT doc_id, spec_version FROM pc_documents WHERE doc_id IN (?, …)
-- spec 4 行 / code 6 行 / test 3 行
```

这四步的行数原样写进了上下文包的 `query.traces`（`runs/<run_id>/context.json`），
可用 `python -m sddctx context --task TASK-0007 --json | jq .query.traces` 复核。

检索排序结果（同一次运行）：

| 类别 | Top 结果（score） |
| --- | --- |
| spec | `SPEC-003#3.4` 72.05 → `SPEC-003#3.2` 55.41 → `SPEC-003#3.3` 37.60 → `SPEC-001#1.2` 22.21 |
| code | `refund.py::RefundService.quote_partial_refund` 56.51 → `refund.py::<module>` 39.38 → `money.py::share` 30.42 → `pricing.py::member_discount` 29.79 |
| test | 目标测试文件 6 个用例 + 文件级说明全部进入前 7，其它测试文件排在后面 |

## 4. 数据库真的参与了（三条可核对证据）

1. **不导入就没有上下文**：`init` 后直接 `context` 会得到空来源与 `必需来源齐全：否`；
   `ingest` 之后才有 4/5/7 条来源。
2. **规格换版本，输出跟着换**：`pc_documents.path` 由 `specs/SPEC-003-refund.md` 变为
   `specs/variants/SPEC-003-refund.v2.md`，`spec_version` 由 `v1.0` 变为 `v1.1`，
   上下文的条款正文随之为 v1.1（含新增的尾差条款），见 [`logs/16_context_v2.log`](logs/16_context_v2.log)。
3. **降级是明示的**：把 seekdb 停掉或设 `SDDCTX_DB=sqlite`，`doctor` / `context` 的
   `storage.backend` 会变成 `sqlite` 并给出 `fallback_reason`，不会假装用了数据库。

## 5. 自己复跑

```bash
cd minimax
python -m sddctx init
python -m sddctx ingest --reset
python -m sddctx context --task TASK-0007 --json | python -c "
import json,sys; d=json.load(sys.stdin)
print(d['storage']); [print(k, v['steps']) for k,v in d['query']['traces'].items()]"
```

也可以直接连库核对：

```bash
python -c "
import pymysql
c=pymysql.connect(host='127.0.0.1',port=2881,user='root',password='',database='sddctx')
cur=c.cursor(); cur.execute('select category,count(*) from pc_chunks group by category'); print(cur.fetchall())
cur.execute(\"select path,spec_version from pc_documents where category='spec'\"); print(cur.fetchall())"
```
