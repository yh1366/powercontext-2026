# StudyMind - 智学伴侣

基于 **PowerContext** 记忆引擎 + **seekdb** 混合检索的智能学习助手。

## 项目简介

StudyMind 解决学习过程中的核心痛点：笔记分散难检索、学习进度无法追踪、薄弱知识点容易遗忘。通过 PowerContext 实现学习记忆的持久化存储与跨会话复用，通过 seekdb 实现知识资料的语义+关键词混合检索，让学习助手"越用越懂你"。

## 核心功能

| 功能 | 说明 | 技术支撑 |
|------|------|----------|
| 知识库管理 | 添加、管理学习资料（笔记、论文摘要、概念） | seekdb 向量存储 |
| 智能检索 | 语义搜索 + 关键词搜索 + 混合检索 | seekdb Hybrid Search |
| 学习进度追踪 | 记录各学科/主题的完成度 | PowerContext Memory |
| 薄弱知识点记录 | 标记需要加强的知识领域 | PowerContext Memory |
| 个性化推荐 | 基于学习历史推荐相关内容 | PowerContext Recall |
| 智能问答 | 结合知识库和学习记录回答问题 | PowerContext + seekdb |
| 学习会话连续性 | 跨会话保持学习上下文 | PowerContext Handoff |
| 自我测试 | 从知识库自动生成测试题 | seekdb 检索 |

## 技术架构

```
┌─────────────────────────────────────────────────┐
│                   Frontend                       │
│            HTML / CSS / JavaScript               │
└────────────────────┬────────────────────────────┘
                     │ HTTP API
┌────────────────────▼────────────────────────────┐
│               Flask Backend                      │
│  ┌──────────────┐    ┌───────────────────────┐  │
│  │ PowerContext │    │     seekdb Client     │  │
│  │   Client     │    │  (pyseekdb + OpenAI)  │  │
│  └──────┬───────┘    └──────────┬────────────┘  │
└─────────┼──────────────────────┼────────────────┘
          │                      │
┌─────────▼─────────┐  ┌────────▼────────────────┐
│  PowerContext     │  │     seekdb (OceanBase)   │
│  Server           │  │     向量 + 全文 + 结构化  │
│  - Memory         │  │     混合检索引擎          │
│  - Experience     │  │                          │
│  - Handoff        │  │                          │
│  - Scope          │  │                          │
└───────────────────┘  └─────────────────────────┘
```

## 技术分工

### PowerContext 的作用
- **Memory（记忆）**：存储学习进度、薄弱知识点、搜索历史等，支持语义召回
- **Experience（经验）**：记录每次学习活动的情况、行动、结果和教训
- **Handoff（交接）**：在学习会话之间保持上下文连续性
- **Scope（作用域）**：为每个用户/学习项目隔离上下文

### seekdb 的作用
- **向量检索**：将学习资料向量化，支持语义相似度搜索
- **全文检索**：基于 IK 分词器的关键词精确匹配
- **混合检索**：一条 SQL 同时执行向量+全文+结构化过滤
- **知识存储**：持久化存储所有学习资料及其元数据

## 快速开始

### 环境要求

- Node.js 18+（推荐）或 Python 3.11+
- PowerContext Server（可选，有内置 fallback）
- seekdb（可选，有内置 fallback）

### 安装步骤（Node.js 版本，推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/yh1366/powercontext-2026.git
cd powercontext-2026/qoder

# 2. 安装 Node.js 依赖
npm install

# 3. 配置环境变量（可选）
cp .env.example .env
# 编辑 .env 文件，填入 PowerContext/seekdb 地址

# 4. 启动应用
npm start

# 访问 http://localhost:5000
```

### 安装步骤（Python 版本）

```bash
# 1. 克隆仓库
git clone https://github.com/yh1366/powercontext-2026.git
cd powercontext-2026/qoder

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入你的配置

# 4. (可选) 启动 PowerContext Server
uv tool install --force "powercontext[cli,server]==1.4.1"
powercontext config init
powercontext server run

# 5. (可选) 启动 seekdb
docker run -d --name seekdb -p 2881:2881 -v ./data:/var/lib/oceanbase/store oceanbase/seekdb:latest

# 6. 启动应用
cd backend
python app.py
```

### 最小化运行（无需外部依赖）

项目内置了 fallback 机制，即使不部署 PowerContext 和 seekdb，也能以本地内存模式运行全部功能：

**Node.js 版本（推荐）：**
```bash
cd qoder
npm install
npm start
```

**Python 版本：**
```bash
cd qoder
pip install flask flask-cors python-dotenv requests
cd backend
python app.py
```

访问 http://localhost:5000 即可使用。

## 验证样例

### 样例 1：知识库检索

**初始数据**：点击"加载示例数据"按钮

**操作步骤**：
1. 进入"智能检索"页面
2. 输入关键词："向量检索"
3. 选择检索模式："混合检索"
4. 点击搜索

**预期结果**：
- 返回"向量检索与语义搜索"等相关资料
- PowerContext 记录本次搜索行为
- 仪表盘检索次数 +1

### 样例 2：学习进度追踪

**操作步骤**：
1. 进入"学习进度"页面
2. 填写：学科=Python编程，主题=列表推导式，完成度=80%
3. 点击"保存进度"
4. 刷新学习记录

**预期结果**：
- PowerContext Memory 保存进度记录
- 学习记录列表显示新条目
- 进度条可视化展示

### 样例 3：智能问答

**操作步骤**：
1. 先加载示例数据
2. 进入"智能问答"页面
3. 输入问题："什么是数据库索引？"
4. 发送

**预期结果**：
- PowerContext 召回相关学习记忆
- seekdb 检索相关知识资料
- 回答中引用具体资料来源
- 显示检索到的记忆和资料数量

### 样例 4：PowerContext 记忆持久化

**操作步骤**：
1. 添加一条学习资料
2. 记录一个学习进度
3. 重启后端服务
4. 查看学习记录

**预期结果**：
- 如果连接了 PowerContext Server，重启后记忆仍在
- 如果未连接（fallback 模式），记忆在重启后丢失（符合预期）

## 项目结构

```
qoder/
├── README.md                          # 项目说明
├── requirements.txt                   # Python 依赖
├── .env.example                       # 环境变量模板
├── backend/
│   ├── app.py                         # Flask 主应用（API 路由）
│   ├── config.py                      # 配置管理
│   ├── powercontext_client.py         # PowerContext 集成
│   └── seekdb_client.py              # seekdb 集成
├── frontend/
│   ├── index.html                     # 主页面
│   ├── css/
│   │   └── style.css                  # 样式
│   ├── js/
│   │   └── app.js                     # 前端逻辑
│   └── assets/                        # 静态资源
├── data/
│   └── sample_notes/                  # 示例数据
├── docs/
│   ├── setup.md                       # 详细部署指南
│   └── demo.md                        # 演示说明
└── scripts/
    └── init_db.py                     # 数据库初始化脚本
```

## Qoder 开发记录

本项目使用 Qoder 完成以下开发工作：

1. **需求拆解**：通过 Qoder 对话分析学习场景痛点，确定功能范围
2. **架构设计**：使用 Qoder 设计 PowerContext + seekdb 的集成方案
3. **编码实现**：
   - 后端 Flask API 全部由 Qoder 生成
   - PowerContext Client 封装由 Qoder 完成
   - seekdb Client 集成由 Qoder 完成
   - 前端页面由 Qoder 生成
4. **调试优化**：通过 Qoder 排查 API 连接问题、优化检索逻辑
5. **文档编写**：README、部署指南、验证样例由 Qoder 辅助编写
6. **部署发布**：通过 Qoder Sites 发布项目网站

## 许可证

MIT License
