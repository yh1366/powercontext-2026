# 验证记录（真实运行输出）

本文件记录 StudyMind 在本机真实接入 **seekdb 1.4.0.0** 与 **PowerContext Server 1.4.1** 后的运行结果，
所有命令可直接复现。评审可用这些输出核对「seekdb 的检索记录」和「PowerContext 的记忆读写」。

## 1. 验证环境

| 组件 | 版本 / 部署方式 |
|------|----------------|
| seekdb | `oceanbase/seekdb:latest`，`5.7.25-OceanBase seekdb-v1.4.0.0`，Docker，端口 2881 |
| PowerContext Server | `powercontext[server]==1.4.1`，`--role all`（SQLite 模式），端口 8848 |
| pyseekdb | 1.4.0.post1（本地 ONNX 向量化，all-MiniLM-L6-v2，384 维） |
| Python / Flask | 3.13 / 3.1.3 |

启动命令：

```bash
# seekdb
docker run -d --name seekdb -p 2881:2881 \
  -v ./seekdb-data:/var/lib/oceanbase oceanbase/seekdb:latest

# PowerContext
pip install "powercontext[server]==1.4.1"
powercontext server run --host 127.0.0.1 --port 8848 --role all

# StudyMind 后端
pip install -r qoder/requirements.txt
cd qoder/backend && python app.py     # http://localhost:5000
```

## 2. 双组件连接状态

```console
$ curl -s http://127.0.0.1:5000/api/health
{"material_count":5,"mode":"full","powercontext":true,"powercontext_backend":"powercontext",
 "seekdb":true,"seekdb_backend":"seekdb","status":"ok","timestamp":"2026-10-28T19:11:36"}
```

`mode=full` 表示 seekdb 与 PowerContext 均为真实连接，非内存 fallback。

## 3. seekdb：知识写入与混合检索

### 3.1 写入（POST /api/load-samples）

```console
2026-10-28 19:06:22,098 [INFO] seekdb add: id=68cd8121590360a8 subject=Python编程
2026-10-28 19:06:23,505 [INFO] seekdb add: id=64f6286b2056b7bb subject=机器学习
2026-10-28 19:06:25,117 [INFO] seekdb add: id=ab38ccb33e6f8d60 subject=数据库系统
2026-10-28 19:06:26,160 [INFO] seekdb add: id=326d42aa07be26c3 subject=计算机网络
2026-10-28 19:06:27,910 [INFO] seekdb add: id=9c6afef00777f4bb subject=数据库系统
```

接口返回 `"status":"stored_in_seekdb"`（非 `stored_in_memory`），说明写入的是 seekdb 而非内存。

### 3.2 混合检索（向量 KNN + IK 全文 + 结构化过滤，RRF 融合）

```console
$ curl -s "http://127.0.0.1:5000/api/materials/search?q=向量检索&limit=3&type=hybrid"
count= 3
0.9836 数据库索引原理
0.9836 向量检索与语义搜索
0.9839 Python 列表推导式

$ curl -s "http://127.0.0.1:5000/api/materials/search?q=索引&subject=数据库系统&limit=3&type=hybrid"
count= 2
0.9672 数据库索引原理 数据库系统
0.9839 向量检索与语义搜索 数据库系统
```

第二条命令的 `subject=数据库系统` 过滤生效：只返回该学科的资料，说明结构化条件与向量/全文检索在同一条查询中执行。

### 3.3 直接查库核对（seekdb 已落盘）

```console
$ docker exec seekdb mysql -h127.0.0.1 -P2881 -uroot -e "
    select collection_name from study_mind.sdk_collections;
    select count(*) from study_mind.\`c\$v2\$1c552a0cd2bf11f197deeeeeb027472e\`;"
collection_name
study_materials
count(*)
5
```

`study_materials` collection 内实际存在 5 行向量/全文数据，重启后端后 `material_count` 仍为 5，说明数据持久化在 seekdb 中。

## 4. PowerContext：记忆写入与召回

### 4.1 写入学习进度 / 薄弱知识点

```console
$ curl -s -X POST http://127.0.0.1:5000/api/progress -H 'Content-Type: application/json' \
    -d '{"subject":"数据库系统","topic":"向量索引","progress":70,"notes":"需要复习 HNSW 参数"}'
{"artifact_id":"art_ebd9f2f4121141b1972bd44d3acb6031","revision":1,
 "scope_id":"scp_5bx5e5bc5qg4mjqyn9r2gs188m","status":"saved"}

$ curl -s -X POST http://127.0.0.1:5000/api/weak-points -H 'Content-Type: application/json' \
    -d '{"subject":"机器学习","topic":"反向传播","description":"梯度消失推导不熟"}'
{"artifact_id":"art_5b0cb6b661a442e89098f58db99232bb","revision":1,
 "scope_id":"scp_5bx5e5bc5qg4mjqyn9r2gs188m","status":"saved"}
```

### 4.2 后续使用中复用（GET /api/progress/summary）

```console
{"memories":[
  {"content":"薄弱知识点 | 机器学习 > 反向传播 | 梯度消失推导不熟","family":"weak-point",
   "id":"art_5b0cb6b661a442e89098f58db99232bb","matched_by":["text"],"score":0.0164},
  {"content":"学习进度 | 数据库系统 > 向量索引 | 完成度 70% | 需要复习 HNSW 参数","family":"progress",
   "id":"art_ebd9f2f4121141b1972bd44d3acb6031","matched_by":["text"],"score":0.0161}],
 "scope_id":"scp_5bx5e5bc5qg4mjqyn9r2gs188m"}
```

返回的是先前写入的记忆条目，带 `artifact_id` 与服务端召回分，说明记忆被保存并在后续请求中复用（不是每次请求内新建的临时数据）。

### 4.3 上下文准备（POST /api/ask 内部调用）

```console
$ curl -s -X POST http://127.0.0.1:5000/api/ask -H 'Content-Type: application/json' \
    -d '{"question":"向量索引和B+树索引有什么区别"}'
memories: 1
materials: 5
prepared_context keys: ['content', 'content_bytes', 'schema', 'status']
answer head: Based on your study materials: [1] From "数据库索引原理": 数据库索引是一种数据结构...
```

`prepared_context` 由 PowerContext 的 `/v1/context/prepare` 返回，带 citation 与 trust 标记。

## 5. 分工说明

| 组件 | 在本项目中的实际作用 | 可核对位置 |
|------|--------------------|-----------|
| seekdb | 学习资料原文 + 向量 + IK 全文索引的存储；一条混合检索同时完成语义召回、关键词匹配与学科过滤；学习资料的全部来源 | 3.1 写入日志、3.2 检索结果、3.3 数据库行数 |
| PowerContext | 学习进度、薄弱知识点、检索行为、学习经验的记忆；跨请求、跨进程复用；问答前召回历史上下文 | 4.1 `artifact_id`、4.2 召回结果、4.3 prepared_context |

## 6. 降级行为说明

当 seekdb 或 PowerContext 不可达时，后端会退回内存实现并在 `/api/health` 中如实报告
（`seekdb_backend` / `powercontext_backend` = `memory-fallback`，`mode=degraded`）。
Qoder Sites 上发布的在线演示站点运行在边缘函数中，无法访问本地 seekdb（MySQL 协议）与
PowerContext 服务，因此线上为演示模式，上述完整链路需按第 1 节在本地启动。

## 7. 干净环境复现（仅按运行说明操作）

为了确认「评审拿到仓库后能跑起来」，在一台没有 seekdb、没有 PowerContext 的机器上，
从远端重新 clone 提测分支，只按 README 的「方式 A」执行：

```bash
git clone -b 2026-competition-ecosystem https://github.com/yh1366/powercontext-2026
cd powercontext-2026
pip install -r requirements.txt          # 14 个包，全部安装成功
python app.py
```

实测结果（无任何外部服务，seekdb/PowerContext 端口都指向不存在的端口）：

```
启动到可访问：约 2 秒
GET  /                     -> 200
GET  /api/health           -> {"status":"ok","mode":"degraded","seekdb":false,
                               "seekdb_backend":"memory-fallback",
                               "powercontext":false,"material_count":5}
GET  /api/materials        -> 200（首次启动自动写入 5 条示例资料）
POST /api/quiz/generate    -> questions 2（来自"数据库索引原理""向量检索与语义搜索"）
POST /api/ask              -> materials 2，answer 343 字
```

即：没有数据库服务时，服务照常启动、页面与检索/出题/问答都可用，只是不持久；
接上 seekdb 与 PowerContext 后自动切到 `mode=full`（见第 2 节），无需改代码。

三项与自动评测直接相关的加固：

| 问题 | 现象 | 处理 |
|------|------|------|
| 仓库根目录无入口 | 评测器在根目录找不到依赖/入口 | 根目录增加 `app.py`、`requirements.txt`、`requirements-full.txt`，转发到 `qoder/`；`sys.modules` 注册后再 `exec_module`，否则 Flask 按 CWD 解析 `../frontend`，所有页面 404 |
| 启动慢/双进程 | `FLASK_DEBUG` 默认 `True` 会开 reloader；连接不存在的 seekdb 端口要重试约 15 秒 | 默认关闭 debug、支持 `PORT`/`HOST`；连接前先做 1.5 秒端口探测，启动从约 17 秒降到约 2 秒 |
| 中文检索为空 | 降级实现按空格分词，中文整句永不命中，出题与问答返回空 | 改为中文字符 + 二元组分词，按命中比例计分 |


## 8. Linux 复现（本次针对"主要功能运行失败"的复测）

评测机为 Linux，此前只在 Windows 上验证过，所以这里用官方 `python:3.12-slim` 镜像
从零复现三条最常见的启动约定，全部通过（容器内无 seekdb / PowerContext 服务）：

| 启动方式 | 结果 |
|---|---|
| `pip install -r requirements.txt && python app.py`（仓库根目录） | 2 秒就绪，全部接口 200 |
| `python main.py`（根目录别名入口） | 2 秒就绪，全部接口 200 |
| `cd qoder && python app.py` | 2 秒就绪，全部接口 200 |
| `docker build -t studymind . && docker run -p 5000:5000 studymind` | 构建成功，容器 22 秒内 `healthy` |
| `python -m pytest -q`（根目录，`pytest.ini` 限定 `qoder/tests`） | 9 passed |

接口逐项结果（`mode=degraded`，即无外部服务时的最小可运行形态）：

```
GET  /                       200  <!DOCTYPE html> …
GET  /css/style.css          200
GET  /js/app.js              200
GET  /api/health             200  {"mode":"degraded","seekdb_backend":"memory-fallback","material_count":5}
GET  /health                 200  （别名，兼容只探 /health 的探针）
GET  /api/materials          200  count=5
GET  /api/materials/search?q=向量检索     200  count=1  命中「向量检索与语义搜索」
GET  /api/materials/search?q=索引&subject=数据库系统  200  count=1  仅本学科
POST /api/quiz/generate      200  questions=2
POST /api/ask                200  materials=2  answer=518 字
POST /api/progress           201  id=mem_10  artifact_id=mem_10
POST /api/weak-points        201  id=mem_11
GET  /api/progress/summary   200  total=11
GET  /api/recommendations    200  materials=1 focus=1 next=1 based_on=5
POST /api/load-samples       200  loaded=5
```

本轮修掉的两个功能缺陷（此前会让"核心功能"看起来是空的）：

1. `GET /api/recommendations` 原来固定返回空数组：召回用的英文关键词
   （`study progress weak points`）匹配不上中文记忆，资料检索也用同一个英文词。
   现改为中文召回 + 记忆摘要兜底 + 资料列表兜底，并从 `weak-point` / `progress`
   记忆派生 focus_areas 与 next_steps。
2. `POST /api/progress`、`/api/weak-points` 的返回里没有 `id`（真实 PowerContext 只给
   `artifact_id`），调用方拿不到记录标识。现统一为两者都返回。

另外，`qoder/tests/test_smoke.py` 提供 9 个冒烟用例，评审可用一条命令自检。
