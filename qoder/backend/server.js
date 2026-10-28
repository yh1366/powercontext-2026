/**
 * StudyMind Backend Server
 * Learning assistant powered by PowerContext + seekdb
 */

const express = require('express');
const cors = require('cors');
const path = require('path');
const { PowerContextClient, FallbackMemoryStore } = require('./powercontext_client');
const { SeekDBClient, FallbackMaterialStore } = require('./seekdb_client');

const app = express();
const PORT = process.env.PORT || 5000;

app.use(cors());
app.use(express.json({ limit: '10mb' }));
app.use(express.static(path.join(__dirname, '..', 'frontend')));

// Initialize clients with fallback
let memoryClient = new PowerContextClient();
let materialClient = new SeekDBClient();

// Check health and switch to fallback if needed
(async () => {
  const pcOk = await memoryClient.healthCheck();
  if (!pcOk) {
    console.log('PowerContext unavailable, using in-memory fallback');
    memoryClient = new FallbackMemoryStore();
  }
  const sdbOk = await materialClient.healthCheck();
  if (!sdbOk) {
    console.log('seekdb unavailable, using in-memory fallback');
    materialClient = new FallbackMaterialStore();
  }
})();

let searchCount = 0;

// ============ Frontend Routes ============

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, '..', 'frontend', 'index.html'));
});

// ============ Health Check ============

app.get('/health', async (req, res) => {
  const pcHealth = await memoryClient.healthCheck();
  const sdbHealth = await materialClient.healthCheck();
  res.json({
    status: 'ok',
    powercontext: pcHealth,
    seekdb: sdbHealth,
    timestamp: new Date().toISOString(),
  });
});

// ============ Study Material APIs ============

app.post('/materials', async (req, res) => {
  const { title, content, subject, topic, type, tags } = req.body;
  if (!title || !content || !subject || !topic) {
    return res.status(400).json({ error: 'Missing required fields: title, content, subject, topic' });
  }

  const result = await materialClient.addMaterial({ title, content, subject, topic, type: type || 'note', tags: tags || [] });

  await memoryClient.saveMemory({
    content: `Study material: ${title} (${subject} > ${topic})`,
    memoryType: 'topic',
    metadata: { subject, topic, material_id: result.id, title },
  });

  res.status(201).json(result);
});

app.get('/materials/search', async (req, res) => {
  const { q, subject, limit = 5, type = 'hybrid' } = req.query;
  if (!q) {
    return res.status(400).json({ error: 'Query parameter q is required' });
  }

  const results = await materialClient.search(q, { subject: subject || null, limit: parseInt(limit), searchType: type });
  searchCount++;

  await memoryClient.saveMemory({
    content: `Search: "${q}" in ${subject || 'all subjects'}, found ${results.length} results`,
    memoryType: 'insight',
    metadata: { query: q, subject, result_count: results.length },
  });

  res.json({ query: q, subject: subject || null, count: results.length, results });
});

app.get('/materials', async (req, res) => {
  const { subject } = req.query;
  const materials = await materialClient.getAllMaterials(subject || null);
  res.json({ materials, count: materials.length });
});

// ============ Learning Progress APIs ============

app.post('/progress', async (req, res) => {
  const { subject, topic, progress, notes } = req.body;
  if (!subject || !topic || progress === undefined) {
    return res.status(400).json({ error: 'Missing required fields: subject, topic, progress' });
  }

  const result = await memoryClient.saveLearningProgress(subject, topic, progress, notes || '');
  res.status(201).json(result || { status: 'saved' });
});

app.get('/progress/summary', async (req, res) => {
  const { subject } = req.query;
  const summary = await memoryClient.getLearningSummary(subject || null);
  res.json(summary);
});

// ============ Weak Points APIs ============

app.post('/weak-points', async (req, res) => {
  const { subject, topic, description } = req.body;
  if (!subject || !topic || !description) {
    return res.status(400).json({ error: 'Missing required fields: subject, topic, description' });
  }

  const result = await memoryClient.saveWeakPoint(subject, topic, description);
  res.status(201).json(result || { status: 'saved' });
});

// ============ Recommendations APIs ============

app.get('/recommendations', async (req, res) => {
  const { subject } = req.query;
  const query = `study progress weak points ${subject || ''}`;
  const memories = await memoryClient.recallMemories(query, { limit: 20 });
  const materials = await materialClient.search(subject || 'study', { subject: subject || null, limit: 5 });

  const recommendations = {
    based_on_memories: Array.isArray(memories) ? memories.length : 0,
    suggested_materials: materials.slice(0, 3),
    focus_areas: [],
    next_steps: [],
  };

  if (Array.isArray(memories)) {
    for (const m of memories) {
      const content = typeof m === 'object' ? (m.content || '') : String(m);
      if (content.toLowerCase().includes('weak point')) {
        recommendations.focus_areas.push(content);
      } else if (content.toLowerCase().includes('progress') && content.includes('%')) {
        recommendations.next_steps.push(content);
      }
    }
  }

  await memoryClient.saveExperience({
    situation: `User requested recommendations for ${subject || 'general study'}`,
    action: 'Generated personalized recommendations',
    outcome: `Found ${materials.length} related materials`,
    lesson: `User is focusing on ${subject || 'multiple subjects'}`,
  });

  res.json(recommendations);
});

// ============ Quiz APIs ============

app.post('/quiz/generate', async (req, res) => {
  const { subject, topic, count = 5 } = req.body;
  const query = `${subject || ''} ${topic || ''}`.trim();
  let materials = await materialClient.search(query, { limit: count * 2 });
  if (!materials.length && query) {
    const broadQuery = subject || topic || 'study';
    materials = await materialClient.search(broadQuery, { limit: count * 2 });
  }

  if (!materials.length) {
    return res.status(404).json({ error: 'No materials found for quiz generation', quiz: null });
  }

  const quiz = {
    subject: subject || '',
    topic: topic || '',
    generated_at: new Date().toISOString(),
    questions: materials.slice(0, count).map((mat, i) => ({
      id: i + 1,
      type: 'open_ended',
      prompt: `Explain the key concepts from: ${(mat.content || '').substring(0, 200)}...`,
      source_material: (mat.metadata && mat.metadata.title) || 'Unknown',
      reference_content: (mat.content || '').substring(0, 500),
    })),
  };

  await memoryClient.saveExperience({
    situation: `Quiz requested for ${subject} > ${topic}`,
    action: `Generated ${quiz.questions.length} questions`,
    outcome: 'Quiz created from study materials',
    lesson: `User is testing knowledge on ${topic}`,
  });

  res.json(quiz);
});

// ============ Session APIs ============

app.post('/session/start', async (req, res) => {
  const { subject = 'General', goal = '' } = req.body;
  const context = await memoryClient.getPreparedContext(`study ${subject} ${goal}`);

  if (context) {
    await memoryClient.createHandoff({
      work_description: `Starting study session: ${subject}`,
      next_steps: goal ? [goal] : ['Review weak points', 'Continue progress'],
    });
  }

  const now = new Date();
  res.json({
    session_id: now.toISOString().replace(/[-:T]/g, '').substring(0, 14),
    subject,
    goal,
    context,
    started_at: now.toISOString(),
  });
});

app.post('/session/end', async (req, res) => {
  const { session_id, summary, topics_covered = [], time_spent_minutes = 0 } = req.body;

  await memoryClient.saveMemory({
    content: `Session ${session_id}: ${summary}. Topics: ${topics_covered.join(', ')}. Time: ${time_spent_minutes}min`,
    memoryType: 'topic',
    metadata: { session_id, topics: topics_covered, time_spent: time_spent_minutes },
  });

  await memoryClient.saveExperience({
    situation: `Study session ${session_id} completed`,
    action: `Studied ${topics_covered.length} topics for ${time_spent_minutes} minutes`,
    outcome: summary,
    lesson: `Covered: ${topics_covered.join(', ')}`,
  });

  res.json({ status: 'session_saved', session_id });
});

// ============ Ask / Chat APIs ============

app.post('/ask', async (req, res) => {
  const { question, subject } = req.body;
  if (!question) {
    return res.status(400).json({ error: 'Question is required' });
  }

  const memories = await memoryClient.recallMemories(question, { limit: 10 });
  const materials = await materialClient.search(question, { subject: subject || null, limit: 5 });
  const prepared = await memoryClient.getPreparedContext(question);

  const answer = buildAnswer(question, memories, materials);
  const relatedTopics = extractTopics(memories, materials);

  const response = {
    question,
    context_sources: {
      memories: Array.isArray(memories) ? memories : [],
      materials,
      prepared_context: prepared,
    },
    answer,
    related_topics: relatedTopics,
  };

  await memoryClient.saveExperience({
    situation: `User asked: ${question.substring(0, 100)}`,
    action: 'Searched memories and materials',
    outcome: `Found ${Array.isArray(memories) ? memories.length : 0} memories, ${materials.length} materials`,
    lesson: answer.substring(0, 200),
  });

  res.json(response);
});

function buildAnswer(question, memories, materials) {
  const parts = [];

  if (materials.length > 0) {
    parts.push('Based on your study materials:\n');
    materials.slice(0, 3).forEach((mat, i) => {
      const content = (mat.content || '').substring(0, 300);
      const title = (mat.metadata && mat.metadata.title) || 'Unknown';
      parts.push(`  [${i + 1}] From "${title}": ${content}...\n`);
    });
  }

  if (Array.isArray(memories) && memories.length > 0) {
    parts.push('\nFrom your learning history:\n');
    memories.slice(0, 3).forEach(mem => {
      const content = typeof mem === 'object' ? (mem.content || '') : String(mem);
      parts.push(`  - ${content.substring(0, 200)}\n`);
    });
  }

  if (parts.length === 0) {
    return 'No relevant study materials or learning history found. Try adding some notes first!';
  }

  return parts.join('');
}

function extractTopics(memories, materials) {
  const topics = new Set();
  if (Array.isArray(memories)) {
    memories.forEach(m => {
      const meta = (typeof m === 'object' && m.metadata) || {};
      if (meta.subject) topics.add(meta.subject);
      if (meta.topic) topics.add(meta.topic);
    });
  }
  materials.forEach(mat => {
    const meta = mat.metadata || {};
    if (meta.subject) topics.add(meta.subject);
    if (meta.topic) topics.add(meta.topic);
  });
  return Array.from(topics);
}

// ============ Sample Data Loader ============

app.post('/load-samples', async (req, res) => {
  const samples = [
    {
      title: 'Python 列表推导式',
      content: '列表推导式是 Python 中一种简洁的列表创建方式。语法为 [expression for item in iterable if condition]。例如：[x**2 for x in range(10) if x % 2 == 0] 生成偶数的平方列表 [0, 4, 16, 36, 64]。列表推导式比等价的 for 循环更简洁，通常也更快。',
      subject: 'Python编程',
      topic: '列表与数据结构',
      type: 'concept',
      tags: ['python', '列表', '推导式'],
    },
    {
      title: '机器学习 - 监督学习概述',
      content: '监督学习是机器学习的一种方法，使用标注数据训练模型。训练数据包含输入特征和对应的正确输出（标签）。常见任务包括分类（预测离散标签）和回归（预测连续值）。算法包括线性回归、决策树、SVM、神经网络等。评估指标有准确率、精确率、召回率、F1分数等。',
      subject: '机器学习',
      topic: '监督学习基础',
      type: 'note',
      tags: ['ML', '监督学习', '分类', '回归'],
    },
    {
      title: '数据库索引原理',
      content: '数据库索引是一种数据结构，用于加速查询。常见索引类型：B+树索引（适合范围查询）、哈希索引（适合等值查询）、全文索引（适合文本搜索）。索引通过减少磁盘I/O提高查询速度，但会增加写入开销和存储空间。选择合适的索引需要分析查询模式。',
      subject: '数据库系统',
      topic: '索引与查询优化',
      type: 'concept',
      tags: ['数据库', '索引', 'B+树', '性能优化'],
    },
    {
      title: 'HTTP 状态码总结',
      content: '2xx 成功：200 OK, 201 Created, 204 No Content。3xx 重定向：301 永久重定向, 302 临时重定向, 304 Not Modified。4xx 客户端错误：400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found。5xx 服务端错误：500 Internal Server Error, 502 Bad Gateway, 503 Service Unavailable。',
      subject: '计算机网络',
      topic: 'HTTP协议',
      type: 'note',
      tags: ['HTTP', '网络', '状态码'],
    },
    {
      title: '向量检索与语义搜索',
      content: '向量检索通过将文本转换为高维向量，利用向量间的距离（如余弦相似度）衡量语义相关性。与传统关键词搜索不同，语义搜索能理解同义词和上下文含义。典型流程：1) 使用 Embedding 模型将文档向量化 2) 存入向量数据库 3) 查询时计算相似度排序。应用场景包括 RAG、推荐系统等。',
      subject: '数据库系统',
      topic: '向量数据库',
      type: 'concept',
      tags: ['向量检索', '语义搜索', 'Embedding', 'RAG'],
    },
  ];

  const results = [];
  for (const s of samples) {
    const result = await materialClient.addMaterial(s);
    await memoryClient.saveMemory({
      content: `Sample material loaded: ${s.title} (${s.subject})`,
      memoryType: 'topic',
      metadata: { subject: s.subject, topic: s.topic, title: s.title },
    });
    results.push(result);
  }

  res.json({ status: 'loaded', count: results.length, materials: results });
});

// ============ Start Server ============

app.listen(PORT, () => {
  console.log(`StudyMind server running on http://localhost:${PORT}`);
  console.log(`PowerContext: ${process.env.POWERCONTEXT_API_URL || 'not configured (using fallback)'}`);
  console.log(`seekdb: ${process.env.SEEKDB_HOST || 'not configured (using fallback)'}`);
});
