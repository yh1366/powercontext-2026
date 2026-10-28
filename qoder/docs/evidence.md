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
