# 一页技术说明

## 1. 模块结构

```
任务定义 tasks/TASK-0007.json
        │
        │  ingest：规格(*.md 按小节切) / 代码(ast 按符号切) / 测试(按用例切)
        ▼
┌────────────────────── seekdb / OceanBase（库 sddctx）──────────────────────┐
│ pc_documents 16 行   文档级元数据（类别、路径、规格版本、sha256）             │
│ pc_chunks    76 行   块级正文（symbol / 行号区间 / 内容 / 块长）              │
│ pc_postings 3698 行  倒排表（term → chunk_id, tf）                          │
│ pc_df       1382 行  文档频率（term → df），BM25 的 idf 来源                 │
│ pc_runs / pc_state   上下文包历史 / 最近一次导入与验证摘要                    │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │  SQL：df + postings + chunks + documents
                                ▼
                      retrieve.py（BM25，k1=1.2 b=0.6）
                      + 规格条目号锚点（SPEC-003#3.2）+ 符号/路径加权
                                │
                                ▼
        context.py ──► 上下文包 {spec[4], code[5], test[7], files, state, checks}
                                │                    │
                    ┌───────────┘                    └────────────┐
                    ▼                                             ▼
            runs/<run_id>/context.{json,md}            PowerContext（状态）
            MiniMax（codegen）→ patches/TASK-0007.patch   scope scp_…
                    │                                             │
                    ▼                                             ▼
            demo_project 代码修改 → verify.py（pytest）→ 验证结果写回状态
```

## 2. 上下文选择方式

1. **切块粒度 = 可定位的编辑单位**：规格按 `##` 小节（`SPEC-003#3.2`），代码按 `ast` 符号
   （`demo_project/ordercore/refund.py::RefundService.quote_partial_refund`），测试按用例
   （`…::test_full_refund_returns_all_shipping`）。输出里的 `source_id` 可以直接 grep。
2. **查询构造**：任务标题 + 摘要 + `query_hints` + 目标/回归测试文件名，而不是全文塞进检索器。
3. **打分**：BM25（中文二元组 + 英文标识符 snake/camel 拆分）打底；
   规格条目号（`SPEC-003#3.2`）在导入时按 3 倍计入词频，成为跨文档的强锚点；
   命中代码符号或文件名的块再乘 1.25 / 1.15。
4. **每类配额**：规格 4 条、代码 5 条、测试 7 条。测试类配额刻意覆盖目标测试文件的
   全部 6 个用例 + 文件级说明，避免排序抖动导致漏掉验收用例。
   另外把「必需代码来源所在文件」的**完整内容（带行号 + sha256）**一并放进上下文：
   符号级片段足够回答「改哪里」，但写 unified diff 需要精确行号与逐字上下文行，
   缺了它模型只能猜，补丁就打不上（`files` 字段，见 `minimax-dev-log.md` 的对照）。
5. **可核对**：`checks` 字段把 `tasks/TASK-0007.json` 里声明的「必需来源」逐条比对，
   `python -m sddctx check` 非 0 退出即代表上下文不满足任务要求。

## 3. 各项技术分工（都是"实际参与"，不是装了就写）

| 技术 | 参与点 | 证据 |
| --- | --- | --- |
| seekdb / OceanBase | 16 个文档、76 个块、3698 条倒排全部落库；每次检索都是 4 条 SQL（df / postings / chunks / documents），命中行数写进上下文包的 `query.traces` | [`db-evidence.md`](db-evidence.md)、`docs/logs/04_ingest_v1.log` |
| PowerContext | 每次上下文构建、验证执行、补丁应用都写一条 `[sddctx] {json}` 记忆；新进程读回并折叠成「已完成 / 待办 / 验证结果」 | [`powercontext-evidence.md`](powercontext-evidence.md)、`docs/logs/19_state_show.log` |
| MiniMax | 在上下文包（规格条款 + 待改函数 + 目标文件全文 + 验收/回归测试清单）下生成 `refund.py` 的统一 diff；补丁打不上时把错误回灌重试一轮；原始请求/响应/usage/耗时全部落盘 | [`minimax-dev-log.md`](minimax-dev-log.md)、`runs/<run_id>/minimax.*.json` |

## 4. 关键取舍（为什么这么写）

* **不用向量库**：语料只有 76 个块，BM25 + 规格锚点已经能把 `SPEC-003#3.2/#3.4` 排到前二；
  引入 embedding 需要额外模型服务，反而让评审的复现成本上升。代价是对同义改写不敏感（已写入已知限制）。
* **倒排表显式落库**，不用 `FULLTEXT`：seekdb 的全文索引能力随版本变化，自己建
  `pc_postings + pc_df` 能保证「检索确实查了数据库」，也能在任何 MySQL 兼容库上复跑。
* **状态按事件写**，不写整份快照：PowerContext 单条记忆上限 8192 字节，
  事件流还能天然回答「谁在什么时候把哪一步推进了」。
* **重置用 `DELETE` 而非 `DROP`**：seekdb 的 DDL 是秒级延迟，反复 DDL 会撞 `ob_query_timeout`；
  `init` 只在缺表时建表，`ingest --reset` 只清数据。
* **测试关掉插件自动加载**（`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`）：验证结论不能随评审机器上
  装了什么 pytest 插件而变。

## 5. 一次任务的时间线

| 阶段 | 命令 | 结果 |
| --- | --- | --- |
| 修改前 | `pytest -q demo_project/tests/test_refund_shipping_allocation.py` | 6 failed（运费分摊/累计上限未实现） |
| 上下文 | `sddctx context --task TASK-0007` | 规格 3.2/3.4 + `quote_partial_refund` + 6 个验收用例 |
| 改代码 | MiniMax（`MiniMax-M2.7`，`/v1/chat/completions`）依上下文出 diff → `patches/TASK-0007.patch` | 只改 `ordercore/refund.py`，1 个 hunk，+11 / −4 行 |
| 修改后 | `pytest -q` | 22 passed（目标 6 + 回归 16） |
| 状态 | `sddctx state show` | 4 步已完成、2 条验证记录，来自 PowerContext |
