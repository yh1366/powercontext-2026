# 技术说明：端侧向量化 + seekdb 混合检索的迷你知识库

> 作品：`stepfun/`（Edge KB）｜分支 `2026-competition-ecosystem`｜对应赛题「端侧 AI 推理性能优化与 seekdb 混合检索」

## 1. 结构与数据流

```
浏览器（用户设备）                       本地 Node 服务                 seekdb（Docker / 单机）
┌───────────────────────────┐        ┌──────────────────┐        ┌─────────────────────────┐
│ Transformers.js + ORT Web │        │ 静态资源 + 代理   │        │ HNSW(cosine,512) 向量索引 │
│  bge-small-zh-v1.5 (INT8) │  向量  │ seekdb-js        │  SQL   │ ngram2 全文索引           │
│  切片 → 批处理 → 向量      │ ─────► │ collection.add() │ ─────► │ DBMS_HYBRID_SEARCH(RRF)  │
│  查询向量（加指令前缀）     │ ◄───── │ hybridSearch()   │ ◄───── │ 一次调用返回融合排序结果   │
└───────────────────────────┘  结果   └──────────────────┘        └─────────────────────────┘
```

推理 100% 在浏览器；服务端只做静态托管与 seekdb 读写代理，**不加载模型、不算向量、不合并两路结果**。

## 2. 模型与量化

- 模型：`Xenova/bge-small-zh-v1.5`（上游 `BAAI/bge-small-zh-v1.5`，Xenova 导出 ONNX），24M 参数、512 维、
  CLS 池化 + L2 归一化；查询侧加 bge 官方指令前缀 `为这个句子生成表示以用于检索相关文章：`。
- 量化：`tools/quantize.py` → `onnxruntime.quantization.quantize_dynamic`，
  **QInt8 / per-channel / reduce_range**，量化 `MatMul`（线性层）与 `Gather`（词表 embedding），
  与模型仓库 `quantize_config.json` 的配置一致。命令：`bash tools/quantize.sh`（Docker 内执行）。

| 权重 | 文件 | 体积 | 压缩比 |
|---|---|---|---|
| FP32 基线 | `onnx/model.onnx` | 90.46 MiB（94,851,877 B） | 1.00× |
| **INT8（提交使用）** | `onnx/model_int8.onnx` | **22.90 MiB（24,011,364 B）** | **3.95×** |

INT8 与 FP32 的向量一致性（同一批中文查询，浏览器实测）：

| 查询 | cos(INT8, FP32) |
|---|---|
| 中文全文索引怎么分词 | 0.9931 |
| seekdb 支持哪些向量距离度量 | 0.9928 |
| GPU 加速的浏览器推理 | 0.9929 |

检索质量（`tools/eval.mjs`，40 篇语料 / 18 条人工标注查询 / top-5，混合检索与纯向量两路，
向量与查询由浏览器内对应精度的模型现场计算）：

| 精度 | 纯向量 Recall@5 | 混合 Recall@5 | 纯向量 nDCG@5 / MRR@5 | 混合 nDCG@5 / MRR@5 |
|---|---|---|---|---|
| FP32 | 1.000 | 1.000 | 1.000 / 1.000 | 1.000 / 1.000 |
| INT8 | 1.000 | 1.000 | 1.000 / 1.000 | 1.000 / 1.000 |

即 INT8 相对 FP32：Recall@5 / nDCG@5 / MRR@5 变化 **0.000**（语料 40 篇、每题 1 个标注答案，任务偏简单，
属于「量化未造成可观测下降」的证据而非难度证明；逐条结果在 `docs/eval-report.json`，
更大语料请按 README §4 复跑）。

评测过程还暴露了一个工程要点（已写进自检逻辑）：**批量写入后，向量索引需要约 0.5–3.3 s 才完全可见**
（向量探针在第 1–5 次重试内命中；混合检索会被全文一路掩盖而察觉不到）。因此导入结束后的自检
用「纯向量」探针 + 重试，避免「写完但向量路查不到」的静默失败。

## 3. 端侧运行与降级

- 运行时：Transformers.js 3.7.6 + ONNX Runtime Web 1.22（webgpu 构建），文件全部自托管
  （`web/vendor/transformers.web.min.js`、`web/vendor/ort/*`），运行时不访问 CDN。
  ONNX 以裸模块名引用 ORT，页面用 importmap 指向本地产物。
- 后端选择：`navigator.gpu?.requestAdapter()` 拿到适配器 → `device:'webgpu'`；否则 `device:'wasm'`。
  三条路径都在界面 chip 与提示条写明当前后端与原因，另有「强制 WASM / 强制 WebGPU」下拉框用于验证两条路径。
- WebGPU 运行期失败（驱动/算子不支持）会捕获异常并自动以 WASM 重载，不中断核心流程。
- WASM 多线程：服务端返回 `Cross-Origin-Opener-Policy: same-origin` + `Cross-Origin-Embedder-Policy: require-corp`，
  页面 `crossOriginIsolated === true`，线程数默认 `min(4, 硬件线程)`，可在界面切换 1/2/4 复测。
- 实测环境说明：本次提交的机器**没有可用的 WebGPU 适配器**（headless Chromium `requestAdapter()` 返回 null），
  因此性能数据全部来自 **WASM** 路径；WebGPU 路径已实现并有 `CONFIGS="webgpu:int8" node tools/bench.mjs`
  可直接复测。降级路径（无 GPU → WASM）为本机真实发生的路径，已由功能自测与截图验证。

## 4. seekdb 使用

- 集合结构（`HNSW(512, cosine)` + `ngram2` 全文索引），由 seekdb-js `createCollection` 建表：

```sql
CREATE TABLE `c$v2$<id>` (
  `id` VARCHAR(64) PRIMARY KEY, `document` TEXT, `embedding` VECTOR(512),
  `metadata` JSON, VECTOR INDEX `vidx`(`embedding`) WITH (DISTANCE=cosine, TYPE=HNSW),
  FULLTEXT INDEX `ftidx`(`document`) WITH PARSER ngram2
);
```

- 混合检索：一次 `collection.hybridSearch({ query:{whereDocument:{$contains:q}}, knn:{queryEmbeddings}, rank:{rrf} })`，
  seekdb 走 `DBMS_HYBRID_SEARCH.SEARCH`，`query_string`（全文）+ `knn`（向量）+ RRF 全在库内完成，
  界面「显示库内生成的 SQL」可看到 seekdb 实际执行的语句（由 `DBMS_HYBRID_SEARCH.GET_SQL` 生成）。
- 增量与刷新：写入走 `collection.add()` 分批提交；seekdb 的 HNSW 随写入增量维护，写入返回即可检索
  （实测：写入第 41 条后立即检索命中）。导入结束后 `/refresh` 用最后一条文档做探针，校验 `visible=true`
  并预热索引，避免「写完但查不到」的静默失败。
- 会话/集合状态：服务端按 `x-session-id` 维护 `activeCollection`、累计导入条数、查询次数；前端切换集合同步写回，
  保证连续导入与切换后检索结果与当前集合一致（功能自测覆盖：集合 B 为空时检索返回 0 条）。

## 5. 状态管理与性能

优化项与实测（设备：Windows 11、4 核 CPU、16 GB、无 GPU 适配器；Chromium 130 headless；
语料 40 篇、查询 10 条、`topK=5`；除对比项外其他条件一致，数据为多轮中位数，原始值见 `docs/bench-*.json`）：

| 配置（均为 WASM） | 模型体积 | 文档向量化吞吐 | seekdb 检索（中位数） | 端到端查询（中位数） | 检索质量 |
|---|---|---|---|---|---|
| FP32 基线（batch16） | 90.46 MiB | 3.8 – 7.0 docs/s | 60 – 61 ms | 135 – 136 ms | Recall@5 1.00 |
| **INT8（提交默认，batch16）** | **22.90 MiB** | **6.2 – 9.7 docs/s** | **68 – 85 ms** | **120 – 144 ms** | Recall@5 1.00 |
| INT8 + 单线程（batch16） | 22.90 MiB | 8.8 – 9.7 docs/s | 85 – 274 ms | 120 – 309 ms | 同指标 |
| INT8 + batch1 | 22.90 MiB | 7.2 – 10.6 docs/s | 64 – 85 ms | 112 – 124 ms | 同指标 |

单次导入（40 篇）分阶段实测（INT8/WASM，冷启动 = 新上下文无缓存，热启动 = 同上下文刷新页面）：

| 阶段 | 冷启动 | 热启动 |
|---|---|---|
| 模型加载（下载/解析/预热） | 0.42 – 8.2 s（波动大） | 0.58 – 2.99 s |
| 文档向量化 40 篇 | 5.3 – 8.3 s | 4.5 – 5.9 s |
| 写入 seekdb（含索引维护） | 3.3 – 9.1 s | 3.1 – 5.0 s |
| 刷新自检（向量探针） | 0.3 – 0.7 s，1–2 次命中 | 0.3 – 0.8 s |
| 查询向量化（单条） | 32 – 67 ms | 35 – 102 ms |
| seekdb 混合检索 | 55 – 85 ms（首次查询含冷缓存最高 647 ms） | 67 – 72 ms |
| 端到端（10 次中位数） | 107 – 460 ms | 127 – 309 ms |

> 噪声说明：本机为共享 4 核环境，同一配置重复测量波动可达 2×；个别 300–460 ms 的端到端值出现在
> seekdb 正在合并/删除集合的窗口内（基准脚本已改为测量结束后统一清理集合）。表中只对量级差异下结论。

关键结论（只对量级差异下判断，单机 4 核环境波动较大，原始逐次数据在 `docs/bench-*.json`）：

1. **量化主要收益是体积与加载**：22.90 MiB vs 90.46 MiB（3.95×）；多次实测中 FP32 权重的会话初始化出现过
   29.7–39.6 s 的长尾（90 MB 解析 + WASM 内存分配），INT8 通常在 1–4 s 完成；
   在**没有 VNNI 的 WASM CPU 路径**上，INT8 的吞吐并不优于 FP32（9.2 vs 10.6–13.3 docs/s，差异在噪声内），
   因此默认 INT8 的取舍是「体积/加载优先」，界面同时保留 FP16/FP32 切换，供有 GPU 的机器复测。
2. **批处理收益明确**：batch 1 → 32，向量化吞吐 5.95 → 14.57 docs/s（2.4×）；写入侧差别更大，
   40 篇按单条写入 20.7–23.2 s，batch16 分批写入 2.7–4.1 s（6–8×）。
3. **预热有效**：模型加载后先做一次空推理，把算子编译/显存分配的冷启动成本从用户查询里挪走，
   首个用户查询的端到端延迟因此稳定在百毫秒级。
4. **WASM 多线程在本机没有正收益**（1 线程 10.2 docs/s vs 4 线程 6.8–8.9 docs/s），推测是 4 核实机被
   浏览器渲染/系统进程抢占所致，故保留线程数开关而不是写死。
5. seekdb 检索占端到端延迟的 40%–60%，向量化占 30%–50%，渲染 < 5 ms；下一步优化空间在
   「查询向量化与检索并行」「更小的 query 前缀/更短文本」上。

## 6. 已知限制与后续

- 仅文本语料；图片可扩展为 CLIP 多模态向量 + OCR 文本两路，混合检索接口无需改动。
- 未取得 WebGPU 实测性能（本机无适配器）；代码路径与降级逻辑已实现并可复测。
- 演示语料规模小，指标以「不下降」为目标；更大规模请按 `data/corpus.json` 格式导入后复跑 `tools/eval.mjs`。
