# StudyMind - 智学伴侣

用 **seekdb** 存知识、用 **PowerContext** 记学习状态的学习助手，解决"笔记散、找不到、学了就忘"的问题。

- 在线演示站点（Qoder Sites 发布）：https://studymind-app-arwd9qi4cqr.qoder.zone/
- 代码仓库：https://github.com/yh1366/powercontext-2026（`main` 分支，项目位于 `qoder/` 目录）
- 真实运行验证记录：[`docs/evidence.md`](docs/evidence.md)

> **评审提示**
> 线上站点运行在 Qoder Sites 的边缘函数里，无法连接本地 seekdb（MySQL 协议）与 PowerContext 服务，
> 因此线上是**演示模式**（数据存于边缘函数内存，刷新会重置），`/health` 会如实返回 `mode=degraded`。
> 需要核对 seekdb / PowerContext 的真实读写时，请按下方「快速开始」在本地启动，完整链路已在
> [`docs/evidence.md`](docs/evidence.md) 中留下可复现的输出。

## 解决的问题

学习和复习时反复遇到三件事：

1. 资料散在笔记、PDF、聊天记录里，想找"上次那个向量检索的说明"只能靠翻；
2. 复习进度没有地方记，隔一周就不知道上次学到哪、哪些点没弄懂；
3. 每次重新开始都要重新交代背景，助手不记得你之前卡在哪。

StudyMind 把「知识」和「学习状态」分开存：**seekdb 负责知识检索**，**PowerContext 负责记住你学到哪、哪里薄弱**，
两者在问答和出题时汇合。

## 核心功能

| 功能 | 说明 | 技术支撑 |
|------|------|---------|
| 知识库管理 | 添加学习资料（笔记、论文摘要、概念），持久化存储 | seekdb collection `study_materials` |
| 混合检索 | 一条查询同时做向量语义召回 + IK 关键词匹配 + 学科过滤，RRF 融合 | seekdb Hybrid Search |
| 学习进度追踪 | 记录各学科/主题完成度，跨会话可查 | PowerContext Memory（`kind=progress`） |
| 薄弱知识点 | 标记需要加强的知识点，问答时优先召回 | PowerContext Memory（`kind=weak-point`） |
| 智能问答 | 召回历史记忆 + 检索资料，回答中标注来源 | PowerContext + seekdb |
| 自我测试 | 按学科从知识库抽题，题目带出处 | seekdb 检索 |
| 学习会话交接 | 记录本次学习目标与下一步 | PowerContext Memory（`kind=handoff`） |

## 技术架构

```
┌──────────────────────────────────────────────┐
│  Frontend  HTML / CSS / JS（同一套页面）      │
│   本地：/api/*        线上：/functions/v1/app │
└───────────────────┬──────────────────────────┘
                    │ HTTP
┌───────────────────▼──────────────────────────┐
│  Flask Backend（qoder/backend）               │
│   ├── seekdb_client.py   知识：存取 + 混合检索 │
│   └── powercontext_client.py 记忆：读写 + 召回 │
└─────────┬─────────────────────────┬──────────┘
          │ MySQL 协议 (pyseekdb)   │ HTTP API
┌─────────▼──────────┐   ┌──────────▼────────────┐
│ seekdb             │   │ PowerContext Server   │
│ 向量 + IK 全文 +   │   │ Memory / Scope /      │
│ 结构化过滤         │   │ prepared context      │
└────────────────────┘   └───────────────────────┘
```

### 两个指定技术的分工

| 组件 | 实际作用 | 可核对的痕迹 |
|------|---------|-------------|
| **seekdb** | 学习资料原文、向量、IK 全文索引的存储；混合检索时的语义召回与关键词匹配；学科结构化过滤；出题与问答的资料来源 | 写入日志 `seekdb add: id=...`；`material_count`；库内 `study_materials` 行数 |
| **PowerContext** | 保存学习进度、薄弱点、检索行为、会话交接；下次请求时按相关性召回；问答前生成带 citation 的上下文块 | 写入返回 `artifact_id`；`/api/progress/summary` 召回结果；`/v1/context/prepare` 返回的 prepared context |

具体输出见 [`docs/evidence.md`](docs/evidence.md)。

## 快速开始

项目有三个等价入口（同一份实现，`qoder/backend/app.py`）：

```bash
python app.py                 # 仓库根目录
python main.py                # 仓库根目录的别名入口
cd qoder && python app.py     # 进入项目目录后启动
```

服务默认监听 `0.0.0.0:5000`，同时识别通用的 `PORT` / `HOST` 环境变量，可直接跑在容器里。

### 方式 A：最小运行（不需要 Docker，不需要任何外部服务）

```bash
pip install -r requirements.txt   # 仓库根目录；只装 Web 层，任何 Python 3.10+ 机器都可安装
python app.py
```

打开 http://localhost:5000 即可体验全部页面功能。首次启动会自动写入 5 条示例资料，
因此不接任何外部服务也能检索、出题、记录进度。此时 `/api/health` 返回
`mode=degraded`、`seekdb_backend=memory-fallback`，属于如实报告。

自检（9 个冒烟用例，覆盖检索、出题、进度写入、问答、推荐）：

```bash
python -m pytest -q          # 仓库根目录，pytest.ini 已限定只收集 qoder/tests
```

> 想在这个模式下也用上真实 seekdb 检索：Linux 上执行
> `pip install -r qoder/requirements-full.txt && pip install pylibseekdb`，
> 后端会自动切到嵌入式 seekdb（`seekdb_backend=embedded`），仍然不需要任何服务进程。

### 方式 B：完整模式（真实 seekdb 服务 + PowerContext Server）

#### 环境

- Python 3.10+（验证环境 3.13）
- Docker（运行 seekdb）
- Windows 额外需要 [MSVC 运行库](https://aka.ms/vs/17/release/vc_redist.x64.exe)（onnxruntime 依赖）

#### 1. 启动 seekdb

```bash
docker run -d --name seekdb -p 2881:2881 \
  -v ./seekdb-data:/var/lib/oceanbase oceanbase/seekdb:latest
```

数据库不需要手动建，后端首次启动会自动 `CREATE DATABASE IF NOT EXISTS study_mind`。

#### 2. 启动 PowerContext Server

```bash
pip install "powercontext[server]==1.4.1"
powercontext server run --host 127.0.0.1 --port 8848 --role all
```

首次运行时后端会自动创建一个学习 Scope，并把 `scope_id` 缓存到 `qoder/.powercontext-scope.json`。
确认就绪：`curl http://127.0.0.1:8848/health/ready` → `{"status":"ready",...}`

#### 3. 启动 StudyMind

```bash
pip install -r qoder/requirements-full.txt
SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 \
POWERCONTEXT_API_URL=http://127.0.0.1:8848 \
python app.py
```

访问 http://localhost:5000 ，页面右上角状态应显示 **「PowerContext + seekdb 已连接」**。
`/api/health` 应返回 `mode=full`、`seekdb_backend=server`。

> 连接顺序是自动判断的：设置了 `SEEKDB_HOST` 就走服务端；不设置则优先嵌入式 seekdb；
> 都不可用时退回内存实现，服务仍能启动，`/api/health` 会显示 `mode=degraded`。

### 4. 环境变量（可选）

复制 `qoder/.env.example` 为 `qoder/.env` 后按需修改：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `POWERCONTEXT_API_URL` | `http://localhost:8848` | PowerContext 地址 |
| `POWERCONTEXT_SCOPE_ID` | `default_learning_scope` | 指定已有 Scope；不存在时自动创建 |
| `SEEKDB_HOST` / `SEEKDB_PORT` | `localhost` / `2881` | seekdb 地址 |
| `SEEKDB_DATABASE` | `study_mind` | 自动创建 |

向量化使用 pyseekdb 自带的本地 ONNX 模型（all-MiniLM-L6-v2，384 维），**不需要任何 API Key**。

## 验证样例

### 样例 1：知识写入 + 学科过滤检索

1. 打开 http://localhost:5000 ，点击「加载示例数据」（5 条资料）；
2. 进入「智能检索」，输入 `索引`，学科选 `数据库系统`，模式选「混合检索」；
3. 点击搜索。

**预期**：只返回 `数据库系统` 学科的资料（2 条），其中「数据库索引原理」「向量检索与语义搜索」命中。
**核对**：后端日志出现 5 条 `seekdb add: id=...`；`docker exec seekdb mysql -h127.0.0.1 -P2881 -uroot -e "select count(*) from study_mind.\`c\$v2\$...\`"` 与资料数一致。

### 样例 2：学习进度写入 → 后续复用

1. 进入「学习进度」，填 学科=`数据库系统`、主题=`向量索引`、完成度=`70`，保存；
2. 刷新页面，学习记录列表出现该条。

**预期**：返回体带 `artifact_id`（PowerContext 侧的真实记忆条目），刷新后仍能读到。
**核对**：`curl -s http://127.0.0.1:5000/api/progress/summary` 返回该记忆，含 `matched_by` 召回信息。

### 样例 3：带记忆与出处的问答

1. 先完成样例 1、2；
2. 进入「智能问答」，提问「向量索引和B+树索引有什么区别」。

**预期**：回答引用知识库资料并标注来源标题，同时带出学习历史（进度/薄弱点）；返回体 `context_sources` 里
`materials` 来自 seekdb，`memories` 与 `prepared_context` 来自 PowerContext。

### 样例 4：持久化验证

重启后端进程（不动 seekdb / PowerContext），再次打开页面：资料、进度、薄弱点仍在，
`/api/health` 的 `material_count` 不变。

## Qoder 开发记录

本项目的主要开发工作在 Qoder 中完成，Qoder 承担了需求拆解、架构设计、编码、调试与部署：

| 环节 | Qoder 完成的工作 |
|------|-----------------|
| 需求拆解 | 从"笔记找不到、进度没处记"出发，收敛到知识库 + 记忆两个模块，确定 seekdb/PowerContext 分工 |
| 架构设计 | 设计 Flask 后端 + 双客户端（seekdb / PowerContext）+ 前端单页的整体结构，确定降级策略 |
| 编码 | 生成后端 API、两个客户端封装、前端页面与交互；把项目资产放入赛题要求的 `qoder/` 目录 |
| 调试 | 修正 pyseekdb 1.4 的 `hybrid_search` 调用方式、PowerContext 1.4.1 的真实端点、Flask 静态路由与 `/api/*` 冲突、依赖文件缺失等问题 |
| 部署 | 通过 Qoder Sites 发布站点，处理 Edge Function 路由与静态资源路径 |
| 文档 | 生成 README、部署说明、验证样例与本仓库的验证记录 |

> Qoder 账号 UID：`01a180e8-e634-744f-87dc-5cebe25d65f4`（用于核对站点归属与使用记录）
>
> 对话截图 / 录屏：`docs/qoder/`（提交前把关键环节截图放到该目录）

## 项目结构

```
powercontext-2026/
├── app.py                        # 根目录入口（转发到 qoder/backend/app.py）
├── requirements.txt              # 根目录依赖（= qoder/requirements.txt，最小可安装集）
└── qoder/
    ├── README.md
    ├── requirements.txt          # 最小依赖集（Web 层，任何机器可安装）
    ├── requirements-full.txt     # 完整依赖（+ pyseekdb/onnxruntime，真实检索用）
    ├── .env.example              # 环境变量模板
    ├── backend/
    │   ├── app.py                # Flask API（唯一实现）
    │   ├── config.py             # 配置
    │   ├── seekdb_client.py      # seekdb：存储 + 混合检索
    │   └── powercontext_client.py# PowerContext：记忆读写 + 上下文
    ├── frontend/
    │   ├── index.html
    │   ├── css/style.css
    │   └── js/app.js
    ├── functions/index.ts        # Qoder Sites 边缘函数（线上演示模式）
    ├── docs/
    │   ├── setup.md              # 部署细节
    │   ├── demo.md               # 演示脚本
    │   └── evidence.md           # 真实运行验证记录
    └── scripts/init_db.py        # 可选：初始化示例数据
```

## 已知限制

- 线上站点是演示模式：Qoder Sites 边缘函数无法访问 MySQL 协议（seekdb）与本地 PowerContext 服务，数据存于函数实例内存，刷新或实例回收后会重置。完整功能需本地运行（见「快速开始」）。
- 本地 ONNX 向量模型对中文语义的理解弱于专用中文模型，因此混合检索依赖 IK 全文侧兜底关键词匹配；若需要更强的中文语义召回，可把 `SeekDBClient.embedding_function` 换成中文 embedding 服务。

## 许可证

MIT License
