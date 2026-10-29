# 实测记录（Evidence）

所有数据均来自本提交应用的真实运行（`tools/` 下的脚本 + Playwright 驱动的真实浏览器），
原始 JSON 与截图都在本目录，可按 README §4 的命令复现。

## 0. 测试环境

| 项 | 值 |
|---|---|
| 设备 | Windows 11，4 核 CPU，16 GB 内存，无独立 GPU 适配器（headless Chromium `requestAdapter()` 返回 null） |
| 浏览器 | Playwright 自带 Chromium 130（headless，`--enable-unsafe-webgpu`） |
| seekdb | `oceanbase/seekdb:latest`，版本 `5.7.25-OceanBase seekdb-v1.4.0.0`，Docker 单机，MySQL 协议 2881 |
| 模型 | `Xenova/bge-small-zh-v1.5`（512 维），INT8 22.90 MiB / FP32 90.46 MiB |
| 语料 / 查询 | `data/corpus.json` 40 篇；基准查询 10 条；质量评测 18 条（`data/eval.json`） |
| 已知噪声 | 本机为共享 4 核环境，单轮耗时波动可达 2×；结论只对量级差异下判断，逐次原始值见 `bench-*.json` |

## 1. 量化（tools/quantize.py，Docker 内 onnxruntime 1.20.1）

```json
{
  "method": "onnxruntime.quantization.quantize_dynamic",
  "weight_type": "QInt8", "per_channel": true, "reduce_range": true,
  "op_types_to_quantize": ["MatMul", "Gather"],
  "fp32_bytes": 94851877, "int8_bytes": 24011364,
  "fp32_mib": 90.46, "int8_mib": 22.9, "compression_ratio": 3.95
}
```

仅量化 MatMul（不含词表 Gather）时得到 54.60 MiB / 压缩比 1.66——词表 embedding 占 21 MB+，
是 bge-small 体积的大头，量化时必须包含 `Gather`。

## 2. seekdb 库内混合检索的证据

`POST /api/collections/:name/search` 勾选 `explain` 后返回 seekdb `DBMS_HYBRID_SEARCH.GET_SQL`
实际生成的语句（节选，完整语句见 `functional-report.json` 与界面截图）：

```sql
SELECT ifnull(`_fts`.`_id`, `_vs`.`_id`) as `_id`, ...,
       (ifnull(1 / (`_fts`.`_keyword_rank` + 60), 0)
      + ifnull(1 / (`_vs`.`_semantic_rank` + 60), 0)) as `_score`
FROM ((SELECT *, RANK() OVER(ORDER BY `_keyword_score` DESC) AS `_keyword_rank` FROM (...全文子查询...)) `_fts`
      ... (SELECT *, RANK() OVER(...) AS `_semantic_rank` FROM (...向量 KNN 子查询...)) `_vs`)
ORDER BY `_score` DESC
```

即：**全文一路（`_fts`）与向量一路（`_vs`）在同一条 SQL 内完成 RRF（1/(rank+60)）融合**，
应用层只收到最终排序结果。表上的索引（`SHOW CREATE TABLE`）：
`PRIMARY`、`idx_fts`（FULLTEXT, ngram2）、`idx_vec`（VECTOR, HNSW, cosine, 512）。

## 3. 检索质量（docs/eval-report.json）

```
=== 精度 int8 ===  模型 int8 / wasm / 加载 3272ms
  语料向量化 40 条 · 非法向量 0 · 范数 1.000~1.000
  [hybrid] 索引自检 visible=true 尝试 1 次 / 511ms   → Recall@5=1  nDCG@5=1  MRR@5=1  未命中 0/18
  [vector] 索引自检 visible=true 尝试 2 次 / 658ms   → Recall@5=1  nDCG@5=1  MRR@5=1  未命中 0/18
=== 精度 fp32 ===  模型 fp32 / wasm / 加载 3267ms
  [hybrid] 索引自检 visible=true 尝试 1 次 / 611ms   → Recall@5=1  nDCG@5=1  MRR@5=1  未命中 0/18
  [vector] 索引自检 visible=true 尝试 5 次 / 3322ms  → Recall@5=1  nDCG@5=1  MRR@5=1  未命中 0/18
```

INT8 vs FP32：Recall@5 / nDCG@5 / MRR@5 变化均为 0.000。
向量一致性抽查（同批查询，浏览器内计算）：cos(INT8, FP32) = 0.9931 / 0.9928 / 0.9929。

过程中发现并修复的问题：批量写入后向量索引完全可见需要 1–5 次探针（0.5–3.3 s），
首轮评测曾出现 FP32 纯向量 Recall@5=0.22 的假象（查询打在未完全可见的索引上）；
加入「纯向量探针 + 重试」自检后复测正常。该逻辑已固化在 `/api/collections/:name/refresh`。

## 4. 端到端基准（Playwright，`tools/bench.mjs`，多轮中位数见 `tools/summarize.mjs` 输出）

配置：40 篇语料导入 → 10 条查询 × top5 混合检索；cold = 新上下文（无模型缓存），warm = 同上下文刷新页面。

| 配置 | 轮次 | 模型加载 | 向量化吞吐 | 写入 seekdb | 查询向量化 | seekdb 检索 | e2e 中位数 |
|---|---|---|---|---|---|---|---|
| wasm/int8/b16/auto cold | 4 | 1259 ms | 6.9 docs/s | 2741 ms | 37 ms | 68 ms | 107 ms |
| wasm/int8/b16/auto warm | 4 | 923 ms | 8.8 docs/s | 3417 ms | 49 ms | 71 ms | 127 ms |
| wasm/int8/b16/t1（单线程） | 1 | — | 见 micro-bench | — | — | — | — |
| wasm/int8/b1/auto（批=1） | 1 | — | 7.2–10.6 docs/s | 20754–23205 ms | — | — | 112–124 ms |
| wasm/fp32/b16/auto | 1 | — | 见 micro-bench | — | — | — | — |

首次查询存在 seekdb 冷缓存长尾（单次 647 ms，热后稳定在 55–90 ms），已单独标注。

## 5. 微基准（同会话交替测量，`tools/micro-bench.mjs`，32 条 × 3 轮中位数）

| 精度 | batch | 中位耗时 | 吞吐 |
|---|---|---|---|
| INT8 | 1 | 5378 ms | 5.95 docs/s |
| INT8 | 8 | 3317 ms | 9.65 docs/s |
| INT8 | 16 | 3466 ms | 9.23 docs/s |
| INT8 | 32 | 2196 ms | **14.57 docs/s** |
| FP32 | 1 | 2475 ms | 12.93 docs/s |
| FP32 | 8 | 2402 ms | 13.32 docs/s |
| FP32 | 16 | 3012 ms | 10.62 docs/s |
| FP32 | 32 | 2768 ms | 11.56 docs/s |

结论：batch 1→32 带来 2.4× 吞吐提升；在无 VNNI 的 WASM CPU 路径上 INT8 不比 FP32 快
（量化收益主要在体积 3.95× 与加载长尾），因此默认 INT8 并在界面保留 FP16/FP32 切换。

## 6. 功能验收自测（tools/functional-test.mjs）

17/17 通过（`docs/functional-report.json`），覆盖：seekdb 连接、后端提示、cross-origin isolation、
库内 HNSW/全文索引、写入条数、混合/向量/全文三种检索、库内融合 SQL、集合隔离、
增量写入后向量探针自检、文件切片导入、集合持久化、无 JS 异常、无外部网络请求（无云端推理）。

## 7. 截图（docs/images/）

| 文件 | 内容 |
|---|---|
| `ui-main.png` | 主界面：导入 40 篇后检索「中文全文索引怎么分词？」，RRF 分数、性能面板、后端降级提示 |
| `ui-results.png` | 检索结果区特写 |
| `ui-wasm-int8-b16-{cold,warm}.png` | INT8/WASM 基准运行时界面 |
| `ui-wasm-int8-b1-{cold,warm}.png` | batch=1 对照 |
