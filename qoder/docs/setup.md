# 详细部署指南

## 1. 环境准备

### 1.1 Python 环境

```bash
# 推荐使用 Python 3.11+
python --version

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

### 1.2 安装 PowerContext（可选）

PowerContext 提供学习记忆持久化能力。如果不部署，项目将使用内存 fallback。

```bash
# 方式一：使用 uv（推荐）
uv tool install --force "powercontext[cli,server]==1.4.1"

# 方式二：使用 pip
pip install "powercontext[cli,server]==1.4.1"

# 初始化配置
powercontext config init --language zh --output .env

# 启动服务
powercontext server run --env-file .env

# 验证服务
curl http://localhost:8848/api/v1/system/health
```

配置说明（.env 文件中）：
- `POWERCONTEXT_API_URL`: PowerContext 服务地址，默认 `http://localhost:8848`
- `POWERCONTEXT_SCOPE_ID`: 学习项目 Scope，默认 `default_learning_scope`

### 1.3 安装 seekdb（可选）

seekdb 提供向量+全文混合检索能力。如果不部署，项目将使用内存 fallback。

```bash
# 方式一：Docker（推荐）
docker run -d \
  --name seekdb \
  -p 2881:2881 \
  -v ./seekdb-data:/var/lib/oceanbase/store \
  oceanbase/seekdb:latest

# 方式二：Python SDK（嵌入式模式）
pip install pyseekdb
```

嵌入式模式无需单独部署，seekdb 数据存储在本地 SQLite 文件中。

### 1.4 配置 OpenAI API（可选）

OpenAI API 用于生成文本向量（Embedding），提升语义检索效果。如果不配置，将仅使用关键词检索。

在 `.env` 文件中设置：
```
OPENAI_API_KEY=sk-your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

## 2. 启动应用

```bash
cd qoder

# 安装依赖
pip install -r requirements.txt

# 复制并编辑配置
cp .env.example .env
# 根据实际情况修改 .env

# 启动后端
cd backend
python app.py

# 访问 http://localhost:5000
```

## 3. 通过 Qoder Sites 部署

1. 在 Qoder Desktop 中打开项目
2. 使用 `sites-building` skill 创建站点
3. 使用 `sites-hosting` skill 发布站点
4. 设置可见范围为「任何人都可访问」

## 4. 故障排查

| 问题 | 解决方案 |
|------|----------|
| PowerContext 连接失败 | 检查 `powercontext server run` 是否启动，端口 8848 是否可达 |
| seekdb 连接失败 | 检查 Docker 容器是否运行，端口 2881 是否开放 |
| 向量检索不可用 | 检查 OpenAI API Key 是否正确配置 |
| 页面加载空白 | 检查 Flask 是否启动，前端文件路径是否正确 |

所有外部依赖均为可选，项目可在纯 fallback 模式下运行全部功能。
