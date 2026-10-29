# 规有所依 —— 面向软件持续演进的 SDD 上下文管理系统（MiniMax 赛道）

用 **PowerContext** 保存任务状态、用 **seekdb / OceanBase** 存工程资料并做检索、用 **MiniMax** 完成一次真实代码修改，
把「规格 → 代码 → 测试」这条链做成一个可反复运行、可核对的上下文模块。

被改的业务工程是按揭在 `demo_project/` 里的 `ordercore`（订单计价 / 运费 / 退款），
本次开发任务是 **TASK-0007：部分退款按商品金额占比分摊运费，并限制累计退款不超过实付金额**。

| 技术 | 在本作品里承担的作用 | 接入方式 |
| --- | --- | --- |
| MiniMax | 依据上下文包生成 `demo_project/ordercore/refund.py` 的代码修改 | HTTP `POST /v1/text/chatcompletion_v2`（`MiniMax-M2`），标准库 urllib |
| seekdb / OceanBase | 存规格 / 代码 / 测试三类资料、倒排索引与检索统计量，检索结果直接组成上下文 | MySQL 协议（pymysql），库 `sddctx`，6 张 `pc_*` 表 |
| PowerContext | 保存任务进展、上下文构建记录与验证结果，重启后读回继续使用 | HTTP `/v1/scopes`、`/v1/memory/remember`、`/v1/memory/entries/list`，Server 1.4.1 |

> 目录约定：本赛道全部材料在 `minimax/` 下，仓库根目录的 `app.py` / `qoder/` / `dify/` 属于其它赛道，与本作品无关。

---

## 1. 目录结构

```
minimax/
├── README.md                    ← 本文（运行说明：依赖、配置、初始化、入口、测试、限制）
├── app.py / main.py             ← HTTP 入口（python app.py，默认 127.0.0.1:8000）
├── requirements.txt             ← 运行依赖（只有 pymysql + pytest）
├── pytest.ini                   ← 只收集 demo_project/tests，避免收集到仓库其它目录
├── .env.example                 ← 配置模板（占位值，真实凭据不入库）
├── sddctx/                      ← 上下文模块本体
│   ├── cli.py                   ← 调用入口（CLI）
│   ├── http.py                  ← 调用入口（HTTP）
│   ├── ingest.py                ← 资料导入：分块 → 倒排索引 → 落库（增量）
│   ├── retrieve.py              ← BM25 + 结构化加权检索（SQL 取候选与统计量）
│   ├── context.py               ← 上下文包组装 + 验收自检（必需来源是否齐全）
│   ├── state.py                 ← 任务状态：PowerContext（主）/ 文件（离线降级）
│   ├── pc_client.py             ← PowerContext HTTP 客户端
│   ├── minimax_client.py        ← MiniMax 客户端
│   ├── codegen.py               ← 调 MiniMax 出补丁，落盘请求/响应/快照
│   ├── verify.py                ← 跑目标测试与回归测试，记录命令/退出码/结论
│   ├── demo.py                  ← 验证样例编排（reset / apply / sample）
│   ├── chunking.py / textutil.py / db.py / schema.py
├── specs/                       ← SDD 规格（SPEC-001 计价／002 运费／003 退款／004 审计）
│   └── variants/SPEC-003-refund.v2.md   ← 「规格演进」演示用的 v1.1 版本
├── tasks/TASK-0007.json         ← 任务定义 + 必需来源 + 步骤
├── demo_project/                ← 业务工程（纯标准库，带 pytest 测试）
│   ├── ordercore/{models,money,pricing,shipping,refund}.py
│   └── tests/{test_pricing,test_shipping,test_refund_existing,test_refund_shipping_allocation}.py
├── snapshots/{before,after}/    ← 修改前后代码快照 + manifest.json（sha256）
├── patches/TASK-0007.patch      ← MiniMax 生成的代码差异
├── docs/                        ← 一页技术说明、验证样例、开发记录、数据库与状态记录、测试报告
└── scripts/record_evidence.sh   ← 一键重跑并生成 docs/logs/ 下的全部日志
```

---

## 2. 依赖版本

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.10+（验证于 3.13.14 / 3.13.12） | 只用标准库 + 下列两个包 |
| pymysql | 1.1.0+（验证于 1.2.3） | seekdb / OceanBase 的 MySQL 协议驱动，纯 Python |
| pytest | 8.0+（验证于 9.2.0） | 跑业务工程的目标测试与回归测试 |
| seekdb | `oceanbase/seekdb:latest`（`seekdb-v1.4.0.0`，MySQL 协议端口 2881） | 可用 OceanBase MySQL 模式替代 |
| PowerContext Server | 1.4.1 | `pip install "powercontext[server]==1.4.1"` |
| MiniMax | `MiniMax-M2`（`/v1/text/chatcompletion_v2`） | 需要 Token Plan 的 API Key |

```bash
cd minimax
pip install -r requirements.txt          # 只有 pymysql 与 pytest
```

---

## 3. 配置

所有配置都来自环境变量，也可以写进 `minimax/.env.local`（已在 `.gitignore`，不会入库）：

```bash
cp .env.example .env.local    # 然后把 MINIMAX_API_KEY 等占位值改掉
```

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `SDDCTX_DB` | `auto` | `auto` 先连 seekdb，连不上**如实降级**到 SQLite；`seekdb` / `sqlite` 可强制指定 |
| `SEEKDB_HOST` / `SEEKDB_PORT` | `127.0.0.1` / `2881` | seekdb 连接参数 |
| `SEEKDB_USER` / `SEEKDB_PASSWORD` / `SEEKDB_DATABASE` | `root` / 空 / `sddctx` | 库不存在会自动创建 |
| `SDDCTX_STATE` | `auto` | `auto` 探测 PowerContext，可用就用它，否则降级为本地事件文件 |
| `POWERCONTEXT_API_URL` | `http://127.0.0.1:8848` | PowerContext Server 地址 |
| `MINIMAX_API_KEY` | 空 | 只有 `codegen` 子命令需要，**验证样例不需要** |
| `MINIMAX_BASE_URL` / `MINIMAX_MODEL` | `https://api.minimax.io` / `MiniMax-M2` | MiniMax 接口与模型 |
| `SDDCTX_HOME` | `minimax/runs` | 上下文包、测试日志、状态文件的落盘目录 |

`python -m sddctx doctor` 会打印当前实际生效的数据库、状态后端、MiniMax Key 是否就绪。

---

## 4. 数据初始化（约 2 分钟）

```bash
# 1) seekdb（如果本机已有 OceanBase MySQL 实例可跳过，改 SEEKDB_* 即可）
docker run -d --name seekdb-sdd -p 2881:2881 oceanbase/seekdb:latest
# 首次启动约 2 分钟，出现 "Seekdb started" 后可连

# 2) PowerContext Server（任务状态）
pip install "powercontext[server]==1.4.1"
powercontext server run --host 127.0.0.1 --port 8848 --role all --no-env-file
# 首次启动会初始化 schema，约 30 秒；/health/live 返回 200 即可

# 3) 建表 + 导入工程资料（16 个文档 → 77 个块 → 约 1400 个检索词）
cd minimax
python -m sddctx init
python -m sddctx ingest --reset
```

`init` 只在缺表时建表（seekdb 的 DDL 是秒级延迟，重复建表只会拖慢流程）；
`ingest --reset` 是「清空数据 + 全量重建索引」，保留表结构。
没有 seekdb / 不想起服务时，加 `SDDCTX_DB=sqlite` 即可跑通全链路（会如实打印 `backend: sqlite`）。

---

## 5. 调用入口

### 5.1 命令行（推荐，评审脚本最好写）

```bash
python -m sddctx --help

python -m sddctx doctor                                  # 环境自检
python -m sddctx ingest [--variant v1|v2] [--reset]      # 导入资料（增量）
python -m sddctx context --task TASK-0007                # 组装开发上下文（人读）
python -m sddctx context --task TASK-0007 --json         # 机器可读（含检索 trace）
python -m sddctx check   --task TASK-0007                # 校验上下文是否含任务要求的全部来源
python -m sddctx verify  --task TASK-0007 --phase before # 跑目标测试 + 回归测试，写状态
python -m sddctx state   show --task TASK-0007           # 读回任务状态（PowerContext）
python -m sddctx demo    reset|apply|sample              # 复现「修改前失败 / 修改后通过」
python -m sddctx codegen --task TASK-0007 [--apply]      # 调 MiniMax 生成补丁（需要 Key）
```

### 5.2 HTTP

```bash
python app.py          # 或 python main.py / python -m sddctx serve --port 8000
curl "http://127.0.0.1:8000/api/context?task=TASK-0007&format=json"
curl "http://127.0.0.1:8000/api/state?task=TASK-0007"
curl -X POST http://127.0.0.1:8000/api/verify -d '{"task":"TASK-0007","phase":"after"}'
```

| 接口 | 作用 |
| --- | --- |
| `GET /` | 自检页面（入口列表） |
| `GET /api/health` | 当前数据库 / 状态后端 / 各表行数 |
| `GET /api/context?task=&format=json\|md` | 开发上下文包 |
| `GET /api/state?task=` | 任务状态（已完成 / 待办 / 验证结果） |
| `POST /api/ingest` `{"variant":"v2"}` | 重新导入（规格演进演示） |
| `POST /api/verify` `{"task":"TASK-0007","phase":"after"}` | 跑测试并写状态 |

### 5.3 输出字段（上下文包）

`context --json` 的顶层字段：`task_id` / `spec_variant` / `storage` / `sources.{spec,code,test}` / `state` / `checks`。
每条来源都带 `source_id`（可 grep 的定位符）、`path`、`lines`、`score`、`matched_terms`、`why`：

```json
{
  "source_id": "SPEC-003#3.2",
  "path": "specs/SPEC-003-refund.md",
  "lines": [21, 32],
  "score": 56.8,
  "matched_terms": ["占比", "运费", "分摊"],
  "why": "命中任务关键词 …；条款 SPEC-003#3.2 直接约束本次改动"
}
```

`checks.all_required_present` 对照 `tasks/TASK-0007.json` 里声明的必需来源逐条核对，
是「上下文正确」这条验收项的一键自检（`check` 命令非 0 退出即不通过）。

---

## 6. 测试与验证

```bash
# 业务工程全部测试（22 个：目标 6 + 回归 16）
pytest -q

# 只跑目标功能测试
pytest -q demo_project/tests/test_refund_shipping_allocation.py

# 完整验证样例（含失败→修复→通过→规格变更→状态复用）
python -m sddctx demo sample

# 生成 docs/logs/ 下的全部证据日志
bash scripts/record_evidence.sh
```

`demo sample` 的判定标准（也是验收口径）：

| 阶段 | 命令 | 预期 |
| --- | --- | --- |
| 修改前 | `python -m sddctx demo reset && python -m pytest -q demo_project/tests/test_refund_shipping_allocation.py` | **6 failed**（功能未实现），exit 1 |
| 修改前 | `python -m sddctx verify --task TASK-0007 --phase before` | 目标测试失败、回归测试通过 |
| 上下文 | `python -m sddctx context --task TASK-0007 && python -m sddctx check` | 必需来源齐全，exit 0 |
| 修改后 | `python -m sddctx demo apply && pytest -q` | **22 passed**，exit 0 |
| 规格演进 | `python -m sddctx ingest --variant v2 && python -m sddctx check --variant v2` | 上下文引用 v1.1 条款，exit 0 |
| 状态复用 | `python -m sddctx state show --task TASK-0007` | 读回 4 个已完成步骤 + 两条验证记录 |

详细步骤、预期输出与重置方法见 **[`docs/validation.md`](docs/validation.md)**。

---

## 7. 已知限制

1. **检索是词面检索**（BM25 + 规格条目号锚点 + 符号/路径加权），没有接 embedding 模型，
   因此对「同义改写」不敏感；模块刻意把语料规模（16 文档 / 77 块）控制在毫秒级可枚举的范围，
   用显式倒排表换取可审计、可复现。
2. **中文分词的粒度是二元组**，没有引入 jieba，长专有名词可能被切碎；英文标识符做了
   snake/camel 拆分，所以 `quote_partial_refund` 这类符号命中良好。
3. **seekdb 的 DDL（建表/删除表）是秒级延迟**，`init` 首次建 6 张表约 20 秒；
   重置走 `DELETE` 而不是 `DROP`，避免反复 DDL 触发 `ob_query_timeout`。
4. **PowerContext 单条记忆上限 8192 UTF-8 字节**，所以状态是按事件逐条写入的（一行一条 JSON），
   不是一次性写整份快照。
5. **状态复用 ≠ 恢复完整 Agent 会话**：只恢复「已完成 / 待办 / 验证结果 / 上次上下文编号」，
   题面 2.4 要求的完整会话恢复不在本次实现范围内。
6. `codegen` 需要 `MINIMAX_API_KEY`；**验证样例（`demo sample`）不需要 Key**，
   用的是已提交的 `patches/TASK-0007.patch`。
7. 上下文包的每类保留条数写死为规格 4 / 代码 5 / 测试 7 条（见 `context.DEFAULT_LIMITS`），
   没有做 token 预算动态裁剪。

---

## 8. 文档索引

| 文档 | 内容 |
| --- | --- |
| [`docs/design.md`](docs/design.md) | 一页技术说明：模块结构、上下文选择方式、各项技术分工 |
| [`docs/validation.md`](docs/validation.md) | 验证样例：任务描述、有效规格、初始数据、操作步骤、预期结果、重置方法 |
| [`docs/minimax-dev-log.md`](docs/minimax-dev-log.md) | MiniMax 开发记录：上下文 → 提示词 → 响应 → diff 的对应关系 |
| [`docs/db-evidence.md`](docs/db-evidence.md) | 数据库使用记录：建表、写入行数、检索 SQL 与命中行数 |
| [`docs/powercontext-evidence.md`](docs/powercontext-evidence.md) | 状态写入与复用记录：事件流、重启读回 |
| [`docs/test-report.md`](docs/test-report.md) | 测试报告：命令、退出码、代码版本 |
| [`docs/logs/`](docs/logs/) | 上述每条命令的原始日志（含退出码） |
