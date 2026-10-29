# StudyMind — 学习资料检索与复习记忆助手（Qoder 赛道）

本项目参加 **OceanBase × Qoder「Build with Qoder」** 赛题：用 Qoder 开发一个让学习、科研和工作更高效的项目，
并结合 **seekdb**（资料存储 + 向量/全文/结构化混合检索）与 **PowerContext**（学习记忆与上下文）实现核心功能。

- 项目网站（Qoder Sites 发布）：<https://studymind-app-arwd9qi4cqr.qoder.zone/>
- 项目代码与详细说明：**[`qoder/`](qoder/README.md)**
- 真实运行验证记录：**[`qoder/docs/evidence.md`](qoder/docs/evidence.md)**

> 本仓库根目录的 `dify/`、`gaode/`、`minimax/`、`stepfun/` 是赛事官方模板目录。
> 其中 **`qoder/`** 是本仓库的 Qoder 赛道作品（StudyMind），根目录的 `app.py` / `main.py` /
> `requirements.txt` 只是让评审在任意目录下都能用一条命令启动它。
> **`minimax/`** 是 MiniMax 赛道的作品（「规有所依」SDD 上下文管理系统），
> 入口与说明见 **[`minimax/README.md`](minimax/README.md)**，与 `qoder/` 完全独立、互不影响。

## 快速开始（不需要 Docker，不需要任何外部服务）

```bash
pip install -r requirements.txt   # 仅 Web 层，任何 Python 3.10+ 环境可安装
python app.py                     # 等价写法：python main.py 或 cd qoder && python app.py
```

服务监听 `0.0.0.0:5000`（同时识别 `PORT` / `HOST` 环境变量），首次启动会自动写入 5 条示例资料。
打开 <http://localhost:5000> 即可检索资料、生成练习题、记录学习进度。

```bash
curl http://localhost:5000/api/health
# {"status":"ok","mode":"degraded","seekdb_backend":"memory-fallback","material_count":5,...}
```

`mode=degraded` 表示当前没有连接外部数据库，属**如实上报的降级模式**：功能全部可用，数据存于内存。
按下面「完整模式」启动后，同一接口会返回 `mode=full`、`seekdb_backend=server`。

## 完整模式（真实 seekdb + PowerContext）

```bash
# 1. seekdb（MySQL 协议，端口 2881；库表由后端自动创建）
docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:latest

# 2. PowerContext Server（记忆与上下文）
pip install "powercontext[server]==1.4.1"
powercontext server run --host 127.0.0.1 --port 8848 --role all

# 3. 启动项目
pip install -r qoder/requirements-full.txt
SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 POWERCONTEXT_API_URL=http://127.0.0.1:8848 python app.py
```

依赖 readme / 部署细节见 [`qoder/README.md`](qoder/README.md) 与 [`qoder/docs/setup.md`](qoder/docs/setup.md)。

## 验证样例（可直接复制执行）

| # | 操作 | 命令 | 预期结果 |
|---|---|---|---|
| 1 | 健康检查 | `curl localhost:5000/api/health` | `material_count >= 5`，含 `mode`/`seekdb_backend` |
| 2 | 混合检索 | `curl "localhost:5000/api/materials/search?q=向量检索"` | `count >= 1`，命中「向量检索与语义搜索」 |
| 3 | 学科过滤 | `curl "localhost:5000/api/materials/search?q=索引&subject=数据库系统"` | 结果只含「数据库系统」学科 |
| 4 | 出题 | `curl -X POST localhost:5000/api/quiz/generate -H 'Content-Type: application/json' -d '{"subject":"数据库系统","count":2}'` | `questions` 长度 2 |
| 5 | 记录进度（写记忆） | `curl -X POST localhost:5000/api/progress -H 'Content-Type: application/json' -d '{"subject":"数据库系统","topic":"向量索引","progress":70}'` | 返回 `artifact_id` |
| 6 | 记忆召回 | `curl localhost:5000/api/progress/summary` | `total_memories` 递增，含刚写入的那条 |
| 7 | 个性化推荐 | `curl localhost:5000/api/recommendations` | `suggested_materials` 非空，含 focus/next steps |
| 8 | 问答（记忆 + 资料） | `curl -X POST localhost:5000/api/ask -H 'Content-Type: application/json' -d '{"question":"向量索引和B+树索引有什么区别"}'` | `answer` 非空，`context_sources.materials` 非空 |

完整跑通记录（含 PowerContext `artifact_id`、seekdb 落库行数）见 [`qoder/docs/evidence.md`](qoder/docs/evidence.md)。

## 目录

```
app.py / main.py          # 启动入口（转发到 qoder/backend/app.py，零业务逻辑）
requirements.txt          # 最小依赖（Web 层）
requirements-full.txt     # 完整依赖（pyseekdb / onnxruntime，真实检索用）
Dockerfile                # 容器化启动：docker build -t studymind . && docker run -p 5000:5000 studymind
qoder/                    # ← 本次提交的作品主体
```
