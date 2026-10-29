# Edge KB · 端侧向量化 + seekdb 混合检索

> 2026 OceanBase 大赛「seekdb × 阶跃星辰：端侧 AI 推理性能优化与 seekdb 混合检索」赛道作品。
> 目录：`stepfun/`（分支 `2026-competition-ecosystem`）

一个**完全在浏览器里完成向量化**的迷你知识库：导入文本 → 本地 ONNX 推理生成向量 → 写入 **seekdb** →
查询时在本地生成查询向量 → 由 **seekdb 在库内完成向量 + 全文的 RRF 混合检索** → 展示排序结果与原文。

- 推理位置：用户设备浏览器（ONNX Runtime Web，WebGPU 优先 / WASM 自动降级），**不调用任何云端推理 API**
- 存储与检索：seekdb（`DBMS_HYBRID_SEARCH` + HNSW + `ngram2` 全文索引），**不使用其它数据库或前端内存检索替代**
- 模型：`bge-small-zh-v1.5`（512 维），INT8 动态量化，模型与运行时全部自托管，运行时不访问 CDN

技术说明（一页）：**[docs/tech-note.md](docs/tech-note.md)** ｜ 实测原始数据：**[docs/evidence.md](docs/evidence.md)**

---

## 1. 快速开始

前置：Node.js ≥ 20、Docker（用于跑 seekdb）。

```bash
cd stepfun
bash run.sh                 # 起 seekdb 容器 → 装依赖 → 起应用 → http://localhost:5000
```

`run.sh` 做的事，也可以手动执行：

```bash
# 1) seekdb（MySQL 协议，端口 2881）
docker run -d --name seekdb-edge-kb -p 2881:2881 oceanbase/seekdb:latest

# 2) 依赖 + 模型（首次会下载 INT8 权重 23.9MB；仓库已内置则可跳过）
npm install
bash tools/fetch_model.sh    # 缺失时下载；--quantize 会从 FP32 现场量化

# 3) 启动
npm start                    # 等价：node server/index.mjs
```

打开 <http://localhost:5000>，点「导入示例语料（40 篇）」→ 输入查询 → 查看结果与性能面板。

- 浏览器要求：Chrome / Edge 最新版（WebGPU 可用时自动启用，不可用时自动降级 WASM 并在界面提示）
- 端口/连接可通过环境变量覆盖：`PORT`、`HOST`、`SEEKDB_HOST`、`SEEKDB_PORT`、`SEEKDB_USER`、`SEEKDB_PASSWORD`、`SEEKDB_DATABASE`

```bash
PORT=8080 SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 npm start
curl -s localhost:5000/api/health | head -c 300
```

## 2. 目录结构

```
stepfun/
├── server/                 # Node 服务：静态资源 + seekdb-js 读写代理（不做任何向量计算）
│   ├── index.mjs           #   REST 接口、会话状态、COOP/COEP（WASM 多线程所需）
│   └── store.mjs           #   seekdb-js：建集合、批量写入、库内混合检索、自检
├── web/                    # 前端（原生 ES Module，无构建步骤）
│   ├── index.html          #   界面 + ORT 模块映射（自托管，无 CDN）
│   ├── app.js              #   导入 / 检索 / 性能面板 / 会话状态
│   ├── embedder.js         #   Transformers.js 封装：后端选择、批处理、计时
│   └── vendor/             #   transformers.web.min.js + onnxruntime-web(webgpu) + wasm
├── models/bge-small-zh-v1.5/   # config/tokenizer + onnx/model_int8.onnx（FP32 由脚本下载）
├── data/corpus.json        # 演示语料 40 篇
├── data/eval.json          # 检索质量评测集（18 条查询 + 人工标注）
├── tools/
│   ├── quantize.py/.sh     # ONNX 动态 INT8 量化（Docker 内执行，避免污染本机）
│   ├── fetch_model.sh      # 模型下载（支持 HF_MIRROR）
│   ├── bench.mjs           # Playwright 端到端基准：冷/热启动 × 后端 × 精度
│   └── eval.mjs            # Recall@5 / nDCG@5 / MRR@5 质量评测
└── docs/                   # 技术说明、实测数据、截图
```

## 3. 实现要点

### 3.1 端侧推理与量化

- Transformers.js 3.7.6 + ONNX Runtime Web 1.22（`onnxruntime-web` webgpu 构建），全部文件自托管在 `web/vendor/`。
- 启动时 `navigator.gpu.requestAdapter()` 真实探测：拿到适配器才用 `device: 'webgpu'`，否则 `device: 'wasm'`，
  界面 chip 与提示条写明「已降级 + 原因」，并提供「强制 WASM / 强制 WebGPU」下拉框用于验证两条路径。
- INT8：`tools/quantize.py`（`onnxruntime.quantization.quantize_dynamic`，QInt8、per-channel、reduce_range，
  量化 MatMul 与词表 Gather），压缩比与质量对比见技术说明。
- 查询侧拼接 bge 官方检索指令前缀（`为这个句子生成表示以用于检索相关文章：`），文档侧不加；
  CLS 池化 + L2 归一化，维度 512。

### 3.2 seekdb 数据层

- 每个知识库对应一个 seekdb 集合：`HNSW(dimension=512, distance=cosine)` + 全文索引 `ngram2`（2~3 字切分，中文无需词典）。
- 写入：浏览器算好向量后按批 POST 到服务端，服务端用 **seekdb-js** `collection.add()` 落库（默认 64 条/批，可调）。
- 检索：**一次调用** `collection.hybridSearch()`，走 seekdb 的 `DBMS_HYBRID_SEARCH`——
  `query_string`（全文）+ `knn`（向量）+ `rank.rrf` 在**库内**融合，服务端只转发最终排序结果。
  界面上勾选「显示库内生成的 SQL」可以看到 seekdb 实际执行的语句。
- 增量与刷新：写入返回即可检索（HNSW 增量维护）；导入结束后自动调用 `/refresh` 用最后一条文档做探针，
  校验 `visible=true` 并预热索引。集合与会话状态由服务端按 `x-session-id` 维护。

### 3.3 性能优化

按时间面板分阶段计时（模型加载 / 文档向量化 / 写入 / 刷新 / 查询向量化 / seekdb 检索 / 端到端），
优化项：INT8 权重、批量 embedding（batch 1~32 可调）、预热推理、模型 Cache Storage 缓存、
COOP/COEP 开启 WASM 多线程、写入分批 + 批量提交、查询侧复用已加载会话。
实测数据与前后对比见 [docs/tech-note.md](docs/tech-note.md)。

## 4. 复现测试

```bash
# 端到端基准（冷启动 / 热缓存 × 后端 × 精度），输出 docs/bench-report.json + 截图
npm i -D playwright && npx playwright install chromium
BASE=http://localhost:5000 CONFIGS="wasm:int8" node tools/bench.mjs
BASE=http://localhost:5000 CONFIGS="webgpu:int8" node tools/bench.mjs   # 需要可用 GPU

# 检索质量（Recall@5 / nDCG@5 / MRR@5），输出 docs/eval-report.json
BASE=http://localhost:5000 DTYPES=int8,fp32 node tools/eval.mjs
```

## 5. 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | seekdb 连接状态、集合列表、会话 |
| GET | `/api/models` | 可用 ONNX 权重与体积 |
| GET | `/api/corpus` | 演示语料原文（向量由前端计算） |
| POST | `/api/collections` | 新建集合（HNSW + ngram2） |
| DELETE | `/api/collections/:name` | 删除集合 |
| POST | `/api/collections/:name/documents` | 批量写入向量/原文/元数据 |
| GET | `/api/collections/:name/documents` | 浏览已入库文档 |
| POST | `/api/collections/:name/search` | 混合 / 向量 / 全文检索（`mode`） |
| POST | `/api/collections/:name/refresh` | 索引一致性自检 + 预热 |
| GET | `/api/collections/:name/schema` | 库内表结构与索引 |

## 6. 合规说明

- 前端所有 `fetch` 只访问本机 `localhost` 的静态资源与自身接口，不存在推理请求；服务端不加载任何 embedding 模型。
- 向量、原文、元数据只写 seekdb；检索请求直接下发 seekdb，不在客户端合并两路结果。
- 未预计算任何向量：`data/corpus.json` 只含原文。

## 7. 已知限制

- 无 GPU 的机器会走 WASM（单机实测约每秒数条到十余条，视 CPU 而定），大批量导入建议调大 batch。
- 图片语料为扩展项，当前版本仅文本（见技术说明「后续扩展」）。
- 演示语料仅 40 篇，用于功能与指标复现；规模更大的语料请按 `data/corpus.json` 格式导入。
