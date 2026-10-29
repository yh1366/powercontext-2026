# 记忆酒馆 - Memory Tavern

> 一个会记住你的 NPC 酒馆掌柜。每次对话的关键信息都会通过 PowerContext 持久化存储，
> 下次聊天时 NPC 会回忆起你说过的话、做过的事。

## 作品简介

**记忆酒馆** 是一个基于 Dify + PowerContext 构建的互动式 AI 应用。玩家进入一间奇幻酒馆，
与掌柜"老胡"自由对话。老胡有一个神奇的"恩怨本"——他记得每个客人的名字、说过的话、做过的事。

### 核心玩法

- **跨会话记忆**：关闭对话再回来，老胡依然记得你
- **世界记忆**：老胡会从酒馆的"世界设定"里调出传说、规矩和八卦讲给你听
- **性格演化**：随着互动增多，老胡对你的态度会变化
- **角色扮演**：老胡有鲜明的性格——直爽、记仇、喜欢听故事

### 技术分工

| 组件 | 角色 | 具体作用 |
|------|------|----------|
| **Dify** (必选) | 核心流程编排 | Chatflow 工作流：召回玩家记忆 → 召回世界记忆 → 格式化上下文 → LLM 生成 NPC 回复 → 保存新记忆 → 输出 |
| **PowerContext** (必选) | 记忆与上下文管理 | 两个 Scope：玩家 Scope（跨会话互动记忆，`/v1/memory/search` + `/v1/memory/remember`）与世界 Scope（酒馆 lore/性格/规矩/传闻，`/v1/memory/search`）。工作流每一轮都会**读取两个 Scope 并写入玩家 Scope** |

### 架构图

```
玩家 (Dify Chat UI)
  │
  ▼
Dify Chatflow
  │
  ├─① HTTP Request → 后端 /api/recall       → PowerContext /v1/memory/search（玩家 Scope）
  │                  检索该玩家的历史记忆
  │
  ├─② HTTP Request → 后端 /api/recall-world → PowerContext /v1/memory/search（世界 Scope）
  │                  检索酒馆世界设定（lore/性格/规矩/传闻）
  │
  ├─③ Code Node → 把两路检索结果格式化为 LLM 上下文
  │
  ├─④ LLM Node → 老胡根据 世界设定 + 玩家记忆 + 当前输入 生成回复
  │
  ├─⑤ HTTP Request → 后端 /api/remember → PowerContext /v1/memory/remember（玩家 Scope）
  │                  保存本轮对话关键信息
  │
  └─⑥ Answer → 返回老胡的回复给玩家
```

---

## 运行证据（本次提交附带真实运行记录）

`dify/evidence/` 内为本仓库提交前在 Windows 11 + Python 3.13 上真实运行产生的记录：

| 文件 | 内容 |
|------|------|
| `01-init-powercontext.log` | 初始化脚本输出：解析世界 Scope（服务端返回 `scp_...`）并写入 8 条世界记忆 |
| `02-verify-e2e.log` | `scripts/verify_e2e.py` 全部 PASS：世界记忆召回 4 条命中；会话 A 写入 2 条互动记忆；全新会话 B 召回含"冰霜山脉/雪狼/蜂蜜酒"的原文——**跨会话核心玩法端到端复现**；Scope 幂等重解析一致 |
| `03-test-backend.log` | `backend/test_backend.py` 全部 OK |
| `04-check-dsl.log` | `scripts/check_dsl.py` 全部 PASS：DSL 节点/边一致、世界记忆节点与提示词引用齐全 |
| `05-powercontext-api-transcript.log` | PowerContext HTTP API 原始 JSON 往返（scope 创建幂等、世界 Scope 检索、玩家 Scope 跨会话检索命中原文） |
| `06-powercontext-server-excerpt.log` | PowerContext Server 启动与请求日志摘录 |
| `07-backend-requests.log` | 后端桥接服务访问日志（recall-world/recall/remember/init 均 200） |

复现方式见下文"第五步：一键验证"。

---

## 运行说明

### 环境要求

| 组件 | 版本要求 | 说明 |
|------|----------|------|
| Python | 3.11+ | 后端与脚本运行环境 |
| Dify | 0.15.0+ | 推荐 Docker Compose 部署 |
| PowerContext | `oceanbase/powercontext` 的 `2026-competition-ecosystem` 分支 | 本次验证使用该分支源码启动的 Server（SQLite 存储，无需推理供应商） |
| 模型服务 | 任意 Dify 支持的 LLM | 导入 DSL 后在 Dify 中绑定即可 |

### 第一步：安装并启动 PowerContext

```bash
# 方式 A：pip 安装（需要 uv 包管理器）
pip install uv
uv tool install --force "powercontext[cli,server] @ git+https://github.com/oceanbase/powercontext.git@2026-competition-ecosystem"

# 启动（默认 SQLite，监听 http://127.0.0.1:8000）
powercontext server run
```

```bash
# 方式 B：从源码启动（本次验证采用的方式）
git clone -b 2026-competition-ecosystem https://github.com/oceanbase/powercontext.git
cd powercontext
pip install "fastapi>=0.115,<1" "fastmcp>=3.4,<4" "uvicorn>=0.34,<1" "jinja2>=3.1.6,<4" \
  "markdown-it-py>=4,<5" "opentelemetry-api>=1.30,<2" "opentelemetry-sdk>=1.30,<2" \
  "platformdirs>=4,<5" "prometheus-client>=0.21,<1" "pycasbin>=2.8,<3" \
  "pydantic-settings>=2.7,<3" "scalar-fastapi>=1.8.2,<2" casbin typer aiosqlite \
  "sqlalchemy[asyncio]" "sqlite-vec>=0.1.9,<0.2" pyobvector "pydantic-ai-slim" \
  apscheduler jsonschema pyyaml packaging httpx
PYTHONPATH=src python -c "from powercontext.server.cli import app; app()" run --host 127.0.0.1 --port 8000 --no-env-file
```

验证服务状态：

```bash
curl http://127.0.0.1:8000/health/live
# {"status":"ok"}
```

### 第二步：初始化世界数据

```bash
cd dify
export POWERCONTEXT_URL=http://127.0.0.1:8000
python scripts/init_powercontext.py
```

预期输出（完整记录见 `evidence/01-init-powercontext.log`）：

```
=== Memory Tavern: PowerContext Initialization ===

[1/3] Checking PowerContext connection...
  PowerContext is live at http://127.0.0.1:8000

[2/3] Resolving NPC world scope (idempotency_key=tavern-npc-world)...
  Scope tavern-npc-world -> scp_0z98cs15wjmthgnv1zafnm6j3b

[3/3] Seeding world memories...
  Seeded [lore]: 记忆酒馆坐落在七个国度的十字路口...
  ...
  8/8 world memories written to scope scp_0z98cs15wjmthgnv1zafnm6j3b
```

> **Scope 说明**：PowerContext 的 Scope ID 由服务端生成（`scp_...`）。本应用以固定
> `idempotency_key`（`tavern-npc-world`、`tavern-player-<名字>`）创建 Scope，重复调用
> 幂等返回同一个 Scope，因此脚本与后端都可安全重跑、重启后无需迁移数据。

### 第三步：启动后端桥接服务

```bash
cd backend
pip install -r requirements.txt   # fastapi / uvicorn / httpx / pydantic
# 如 Dify 在 Docker 中运行，需绑定 0.0.0.0
uvicorn app:app --host 0.0.0.0 --port 8600
```

环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `POWERCONTEXT_URL` | `http://127.0.0.1:8000` | PowerContext Server 地址 |
| `POWERCONTEXT_TOKEN` | (空) | PowerContext 认证 Token（本地部署无需设置） |
| `TAVERN_WORLD_SCOPE_KEY` | `tavern-npc-world` | 世界 Scope 的幂等键 |

> 后端访问 PowerContext 的 httpx 客户端已设 `trust_env=False`，即使机器配置了
> `HTTP_PROXY/HTTPS_PROXY`（如 Clash），回环调用也不会被代理劫持。

### 第四步：配置并导入 Dify DSL

1. **启动 Dify**（如尚未部署）：

   ```bash
   git clone https://github.com/langgenius/dify.git
   cd dify/docker
   cp .env.example .env
   docker compose up -d
   ```

2. **配置模型**：登录 Dify → 设置 → 模型供应商，添加至少一个 LLM。

3. **导入 DSL**：创建应用 → 导入 DSL 文件 → 选择 `memory_tavern.yml`。导入后：
   - 修改 **LLM 节点**（"老胡回复"）的模型为已配置的模型；
   - 检查环境变量 `BACKEND_URL`：
     - Dify 在 Docker、后端在宿主机：`http://host.docker.internal:8600`
     - Dify 与后端同机直跑：`http://127.0.0.1:8600`
   - 三个 HTTP 节点（召回记忆 / 召回世界记忆 / 保存记忆）都使用该变量，无需逐个改。

4. **发布应用**，即可在聊天界面体验。

### 第五步：一键验证（无需 Dify 与模型 Key）

```bash
# DSL 结构自检（导入前校验节点/边与世界记忆链路）
python scripts/check_dsl.py

# 端到端验证：完全复刻 Chatflow 的节点序列（跳过 LLM 调用）
export POWERCONTEXT_URL=http://127.0.0.1:8000
export BACKEND_URL=http://127.0.0.1:8600
python scripts/verify_e2e.py
```

`verify_e2e.py` 依次验证：服务健康 → 世界 Scope 检索命中 → 新玩家无记忆 → 会话 A 写入两条
互动记忆 → **全新会话 B 召回原文（跨会话记忆）** → Scope 幂等重解析。全部 PASS 时退出码为 0。
每次运行使用独立的玩家名（时间戳后缀），可重复执行。

---

## 验证样例（Dify 界面体验）

### 样例 1：初次见面 → 跨会话记忆验证

1. 打开 Dify 应用，名字输入：`小明`，发送：`老胡你好！我是小明，第一次来你这酒馆。给我来杯最好的麦酒！`
   - 预期：老胡以初次见面的口吻回应，并顺带讲酒馆传闻（来自世界 Scope）。
2. 继续发送：`我其实是个冒险者，刚从北方的冰霜山脉过来，差点被雪狼咬了！`
   - 预期：老胡记住这个信息（本轮已通过 `/api/remember` 写入玩家 Scope）。
3. **结束会话，新建会话**，名字仍输入 `小明`，发送：`老胡，还记得我吗？`
   - 预期：老胡主动提到"冰霜山脉"和"雪狼"——这是 PowerContext 跨会话检索到的记忆。
   - 对应证据：`evidence/02-verify-e2e.log` 第 [3] 节展示了同样的召回结果。

### 样例 2：世界记忆验证

1. 新会话，名字输入任意新名字，发送：`老胡，听说你这井水能看见未来？`
2. 预期：老胡按世界 Scope 里的 rumor 设定回应（否认但卖双倍价钱），并可聊起金鳞、闹鬼等传闻。
3. 可直接查询世界 Scope 验证数据来源：

```bash
# 先通过幂等键解析世界 Scope 的 scp id
curl -s -X POST http://127.0.0.1:8000/v1/scopes -H "Content-Type: application/json" \
  -d '{"title":"Memory Tavern NPC World","summary":"world","idempotency_key":"tavern-npc-world"}'

# 用返回的 scope_id 检索（原始往返记录见 evidence/05）
curl -s -X POST http://127.0.0.1:8000/v1/memory/search -H "Content-Type: application/json" \
  -d '{"scope_id":"<上一步返回的 scp id>","query":"酒馆 井水 传闻 未来","limit":5}'
```

### 样例 3：恩怨系统验证

1. 新会话，名字输入 `大壮`，发送：`老胡，你这酒馆也太破了吧！`
2. 再聊两句后新建会话，名字仍为 `大壮`，发送：`老胡，今天给我来杯好酒！`
3. 预期：老胡记得上次的冒犯，回应带"记仇"语气。

恢复初始状态：换一个新名字即可获得全新玩家 Scope；世界 Scope 为共享设定，无需重置。
如需彻底重来，删除 PowerContext 数据目录后重启服务并重跑第二步。

---

## 文件说明

| 文件 | 用途 |
|------|------|
| `memory_tavern.yml` | Dify Chatflow DSL（7 节点：开始→召回记忆→召回世界记忆→格式化→老胡回复→保存记忆→输出） |
| `backend/app.py` | 后端桥接服务：Dify HTTP 节点 ↔ PowerContext HTTP API |
| `backend/test_backend.py` | 后端快速自测脚本 |
| `scripts/init_powercontext.py` | 初始化：幂等解析世界 Scope 并写入 8 条设定记忆 |
| `scripts/verify_e2e.py` | 端到端验证（复刻工作流节点序列，证明跨会话记忆） |
| `scripts/check_dsl.py` | DSL 导入前结构自检 |
| `evidence/` | 本次提交附带的真实运行记录（见上表） |

---

## 技术细节

### PowerContext 接入方式

通过 **HTTP API** 接入（后端桥接服务统一调用，Dify 侧只感知后端）：

| 端点 | 用途 | 调用时机 |
|------|------|----------|
| `POST /v1/scopes` | 以幂等键创建/解析 Scope（服务端返回 `scp_` ID） | 首次对话及每轮请求（有缓存） |
| `POST /v1/memory/remember` | 保存对话关键信息（单条 ≤8192 UTF-8 字节） | 每轮对话结束 |
| `POST /v1/memory/search` | 检索玩家 Scope 与世界 Scope | 每轮对话开始 |

### Scope 设计

- **世界 Scope**（幂等键 `tavern-npc-world`）：老胡的性格来历、酒馆 lore、铁律、传闻。工作流每轮检索并注入系统提示词。
- **玩家 Scope**（幂等键 `tavern-player-<名字 slug>`）：每位玩家的独立互动记忆，实现跨会话个性化。

### Dify 工作流节点

1. **开始**：接收 `player_name`
2. **召回记忆**（HTTP）：POST `/api/recall` → 玩家 Scope 检索
3. **召回世界记忆**（HTTP）：POST `/api/recall-world` → 世界 Scope 检索
4. **格式化记忆**（Code）：合并两路结果为 `memory_context` + `world_context`
5. **老胡回复**（LLM）：系统提示词包含世界设定与玩家记忆两个动态段
6. **保存记忆**（HTTP）：POST `/api/remember`，写入本轮对话摘要
7. **输出**：返回回复

### 已知限制

1. 检索默认为 FTS 全文模式（见 `evidence/05` 的 `"mode":"fts"`），查询词需与记忆文本有词面重合；配置向量检索后语义匹配更好。
2. 后端为单进程演示服务，Scope 解析结果缓存在内存（重启后按幂等键自动重新解析，数据无损）。
3. `verify_e2e.py` 跳过 LLM 节点；Dify 内的完整对话体验需评审自行配置模型服务。

---

## 许可

本项目遵循大赛提交要求，基于 [PowerContext 2026-competition-ecosystem 分支](https://github.com/oceanbase/powercontext/tree/2026-competition-ecosystem) 开发。
