# 记忆酒馆 - Memory Tavern

> 一个会记住你的 NPC 酒馆掌柜。每次对话的关键信息都会通过 PowerContext 持久化存储，
> 下次聊天时 NPC 会回忆起你说过的话、做过的事。

## 作品简介

**记忆酒馆** 是一个基于 Dify + PowerContext 构建的互动式 AI 应用。玩家进入一间奇幻酒馆，
与掌柜"老胡"自由对话。老胡有一个神奇的"恩怨本"——他记得每个客人的名字、说过的话、做过的事。

### 核心玩法

- **跨会话记忆**：关闭对话再回来，老胡依然记得你
- **性格演化**：随着互动增多，老胡对你的态度会变化
- **江湖八卦**：老胡会分享酒馆里的传说和趣闻
- **角色扮演**：老胡有鲜明的性格——直爽、记仇、喜欢听故事

### 技术分工

| 组件 | 角色 | 具体作用 |
|------|------|----------|
| **Dify** (必选) | 核心流程编排 | Chatflow 工作流：接收用户输入 → 调用后端检索记忆 → LLM 生成 NPC 回复 → 保存新记忆 → 输出回复 |
| **PowerContext** (必选) | 记忆与上下文管理 | 存储每个玩家的长期记忆（互动历史、关键事实），提供跨会话的记忆检索（`/v1/memory/search`）和保存（`/v1/memory/remember`）能力 |

### 架构图

```
玩家 (Dify Chat UI)
  │
  ▼
Dify Chatflow
  │
  ├─① HTTP Request → 后端 /api/recall → PowerContext /v1/memory/search
  │                     检索玩家历史记忆
  │
  ├─② Code Node → 格式化记忆为上下文字符串
  │
  ├─③ LLM Node → 老胡根据记忆 + 当前输入生成回复
  │
  ├─④ HTTP Request → 后端 /api/remember → PowerContext /v1/memory/remember
  │                     保存本次对话关键信息
  │
  └─⑤ Answer → 返回老胡的回复给玩家
```

---

## 运行说明

### 环境要求

| 组件 | 版本要求 | 说明 |
|------|----------|------|
| Python | 3.11+ | 后端服务运行环境 |
| Dify | 0.15.0+ | 推荐使用 Docker Compose 部署 |
| PowerContext | 1.0.0+ | 记忆管理服务 |
| 模型服务 | 任意 Dify 支持的 LLM | 如 OpenAI GPT-4o-mini、DeepSeek 等 |

### 第一步：安装并启动 PowerContext

```bash
# 安装 PowerContext（需要 uv 包管理器）
pip install uv
uv tool install --force "powercontext[cli,server] @ git+https://github.com/oceanbase/powercontext.git@master"

# 启动 PowerContext Server（默认监听 http://127.0.0.1:8000）
powercontext server run
```

另开一个终端验证服务状态：

```bash
curl http://127.0.0.1:8000/health/live
# 应返回正常响应
```

### 第二步：初始化世界数据

```bash
# 设置 PowerContext 地址（默认值即为 http://127.0.0.1:8000）
export POWERCONTEXT_URL=http://127.0.0.1:8000

# 运行初始化脚本，创建 NPC 世界 Scope 并写入预设记忆
python scripts/init_powercontext.py
```

预期输出：

```
=== Memory Tavern: PowerContext Initialization ===

[1/3] Checking PowerContext connection...
  PowerContext is live at http://127.0.0.1:8000

[2/3] Creating NPC world scope...
  Scope ready: tavern-npc-world

[3/3] Seeding world memories...
  Seeded [lore]: The Memory Tavern sits at the crossroads of seven realms...
  Seeded [lore]: Old Hu, the tavern keeper, once served a dragon...
  ...

=== Done! Memory Tavern world is ready. ===
```

### 第三步：启动后端桥接服务

```bash
cd backend

# 安装依赖
pip install -r requirements.txt

# 启动服务（默认端口 8600）
# 如果 Dify 运行在 Docker 中，后端需要绑定 0.0.0.0 以便容器访问
uvicorn app:app --host 0.0.0.0 --port 8600
```

环境变量说明：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `POWERCONTEXT_URL` | `http://127.0.0.1:8000` | PowerContext Server 地址 |
| `POWERCONTEXT_TOKEN` | (空) | PowerContext 认证 Token（本地部署无需设置） |

### 第四步：配置并导入 Dify DSL

1. **启动 Dify**（如尚未部署）：

   ```bash
   git clone https://github.com/langgenius/dify.git
   cd dify/docker
   cp .env.example .env
   docker compose up -d
   ```

2. **配置模型**：登录 Dify 管理后台 → 设置 → 模型供应商，添加至少一个 LLM（如 OpenAI）。

3. **导入 DSL**：
   - 进入 Dify → 创建应用 → 导入 DSL 文件
   - 选择 `memory_tavern.yml`
   - 导入后，编辑工作流：
     - 修改 **LLM 节点** 的模型为你已配置的模型
     - 检查 **环境变量** `BACKEND_URL`，确保指向后端服务地址
       - 如果 Dify 运行在 Docker 中且后端在宿主机：使用 `http://host.docker.internal:8600`
       - 如果 Dify 直接运行在宿主机：使用 `http://127.0.0.1:8600`

4. **发布应用**：点击右上角"发布"按钮。

### 网络配置说明

```
┌─────────────────────────────────────────┐
│  Docker Network                         │
│  ┌──────────┐     ┌──────────────────┐  │
│  │  Dify    │────→│ host.docker.     │  │
│  │  Container│     │ internal:8600    │  │
│  └──────────┘     └──────────────────┘  │
└─────────────────────────┬───────────────┘
                          │
┌─────────────────────────┴───────────────┐
│  Host Machine                           │
│  ┌──────────────┐  ┌─────────────────┐  │
│  │ Backend      │──│ PowerContext    │  │
│  │ :8600        │  │ :8000           │  │
│  └──────────────┘  └─────────────────┘  │
└─────────────────────────────────────────┘
```

- Dify 容器内通过 `host.docker.internal` 访问宿主机上的后端服务
- 后端通过 `127.0.0.1:8000` 访问本地 PowerContext Server
- 如果 PowerContext 也运行在 Docker 中，需调整 `POWERCONTEXT_URL` 指向对应容器名或地址

---

## 验证样例

### 样例 1：初次见面 → 跨会话记忆验证

**目标**：验证 PowerContext 能够跨会话保存和检索玩家记忆。

#### 第一轮对话（新会话 A）

1. 打开 Dify 应用，进入聊天界面
2. 在"你的名字"输入框输入：`小明`
3. 发送消息：`老胡你好！我是小明，第一次来你这酒馆。给我来杯最好的麦酒！`

**预期响应**（老胡以初次见面的方式回应）：

> （擦着杯子抬眼看你）嘿，小明是吧？第一次来我这酒馆？得嘞，给你倒一杯咱们的招牌麦酒。
> 喝完这杯，咱就是熟人了——以后来，老胡我保准记得你。

4. 继续对话：`我其实是个冒险者，刚从北方的冰霜山脉过来，差点被雪狼咬了！`

**预期响应**（老胡记住这个信息）：

> （眯起眼睛）嚯！冰霜山脉来的？那可是要命的地界。能活着走到我这儿，说明你小子有两下子。
> 这杯算我请你的——活着就是最大的本事。

5. **结束会话 A**（关闭聊天窗口或新建会话）

#### 第二轮对话（新会话 B — 验证记忆）

1. 重新打开应用，**使用相同的名字** `小明`
2. 发送消息：`老胡，还记得我吗？`

**预期响应**（老胡从 PowerContext 检索到记忆，认出玩家）：

> （放下手中的杯子，仔细打量你）嘿！小明嘛！北方冰霜山脉来的那个冒险者，
> 差点被雪狼咬了那位。我这恩怨本上可记得清清楚楚。来来来，老位置坐好，
> 今儿想聊点什么？

**验证要点**：
- 老胡准确提到了"冰霜山脉"和"雪狼"——这些是第一轮对话中保存的记忆
- 证明 PowerContext 的 `/v1/memory/search` 成功检索到了历史记忆
- 记忆跨会话持久化生效

### 样例 2：恩怨系统验证

**目标**：验证 NPC 对不同行为产生不同的记忆和态度。

1. 新建会话，名字输入：`大壮`
2. 发送：`老胡，你这酒馆也太破了吧！有没有干净点的地方？`
3. 老胡回应（记下这个"冒犯"）
4. 发送：`算了算了，给我来杯酒就是了。`
5. 新建会话，名字仍为：`大壮`
6. 发送：`老胡，今天给我来杯好酒！`

**预期响应**：老胡应该记得上次大壮嫌弃酒馆的事，回应中带有微妙的"记仇"语气。

### 样例 3：PowerContext 数据验证

**目标**：直接通过 PowerContext API 验证记忆数据已正确存储。

```bash
# 查看玩家 Scope 中的记忆条目
curl -X POST http://127.0.0.1:8000/v1/memory/search \
  -H "Content-Type: application/json" \
  -d '{"scope_id": "tavern-player-小明", "query": "冒险 冰霜山", "limit": 10}'

# 预期返回包含"冰霜山脉"、"雪狼"等关键词的记忆条目
```

```bash
# 查看 NPC 世界 Scope 的预设记忆
curl -X POST http://127.0.0.1:8000/v1/memory/search \
  -H "Content-Type: application/json" \
  -d '{"scope_id": "tavern-npc-world", "query": "老胡 性格", "limit": 5}'

# 预期返回关于老胡性格特征的预设记忆
```

---

## 文件说明

| 文件 | 用途 |
|------|------|
| `memory_tavern.yml` | Dify Chatflow DSL 文件，导入即用 |
| `backend/app.py` | Python 后端桥接服务，连接 Dify 与 PowerContext |
| `backend/requirements.txt` | 后端 Python 依赖 |
| `scripts/init_powercontext.py` | PowerContext 初始化脚本（创建 Scope、写入预设记忆） |
| `README.md` | 本文件，运行说明与验证指南 |

---

## 技术细节

### PowerContext 接入方式

本应用通过 **HTTP API** 接入 PowerContext，具体使用以下端点：

| 端点 | 用途 | 调用时机 |
|------|------|----------|
| `POST /v1/scopes` | 创建玩家/NPC 的记忆分区 | 初始化及首次对话 |
| `POST /v1/memory/remember` | 保存对话中的关键信息 | 每轮对话结束后 |
| `POST /v1/memory/search` | 检索相关历史记忆 | 每轮对话开始前 |

### Scope 设计

- **NPC 世界 Scope** (`tavern-npc-world`)：存储老胡的性格设定、酒馆传说、世界规则等共享知识
- **玩家 Scope** (`tavern-player-{名字}`)：存储每位玩家的独立记忆，实现跨会话个性化

### 记忆类型 (kind)

| kind | 说明 | 示例 |
|------|------|------|
| `lore` | 世界背景设定 | 酒馆位于七个王国的十字路口 |
| `personality` | NPC 性格特征 | 老胡记仇但善良 |
| `rule` | 酒馆规则 | 吧台不许带武器 |
| `rumor` | 传闻八卦 | 井水能看见未来 |
| `interaction` | 玩家互动记录 | 小明说他刚从冰霜山脉来 |

### Dify 工作流节点说明

1. **开始节点**：接收 `player_name` 变量
2. **HTTP 召回记忆**：POST 到后端 `/api/recall`，检索玩家历史记忆
3. **格式化记忆**：Python 代码节点，将 JSON 响应转为自然语言上下文
4. **老胡回复**：LLM 节点，使用系统提示词 + 记忆上下文 + 用户输入生成角色化回复
5. **保存记忆**：POST 到后端 `/api/remember`，将本轮对话存入 PowerContext
6. **输出回复**：将老胡的回复返回给玩家

---

## 已知限制

1. **记忆容量**：PowerContext 单条记忆上限 8192 UTF-8 字节，超长对话会被截断
2. **检索精度**：默认使用全文检索（FTS），配置向量检索后可获得更好的语义匹配
3. **并发限制**：后端服务为单进程，适合演示和小规模使用
4. **Dify 版本**：需要 0.15.0+ 以支持 HTTP Request 节点的环境变量引用

---

## 许可

本项目遵循大赛提交要求，基于 [PowerContext 2026-competition-ecosystem 分支](https://github.com/oceanbase/powercontext/tree/2026-competition-ecosystem) 开发。
