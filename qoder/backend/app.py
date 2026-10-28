"""
StudyMind Backend - Learning Assistant with PowerContext + seekdb

Flask API providing:
- Study material management (CRUD + search)
- Learning progress tracking via PowerContext memory
- Personalized recommendations based on learning history
- Quiz generation from stored materials
"""

import os
import sys
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(__file__))
from config import Config
from powercontext_client import PowerContextClient, FallbackMemoryStore
from seekdb_client import SeekDBClient, FallbackMaterialStore

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='../frontend', static_url_path='/static')
CORS(app)

# Initialize PowerContext client (with fallback)
try:
    memory_client = PowerContextClient()
    if not memory_client.health_check():
        logger.warning('PowerContext unavailable, using in-memory fallback')
        memory_client = FallbackMemoryStore()
except Exception as e:
    logger.warning(f'PowerContext init failed: {e}, using fallback')
    memory_client = FallbackMemoryStore()

# Initialize seekdb client (with fallback)
try:
    material_client = SeekDBClient(
        host=Config.SEEKDB_HOST,
        port=Config.SEEKDB_PORT,
        user=Config.SEEKDB_USER,
        password=Config.SEEKDB_PASSWORD,
        database=Config.SEEKDB_DATABASE,
    )
    if not material_client.health_check():
        logger.warning('seekdb unavailable, using in-memory fallback')
        material_client = FallbackMaterialStore()
except Exception as e:
    logger.warning(f'seekdb init failed: {e}, using fallback')
    material_client = FallbackMaterialStore()


# --------------- Frontend routes ---------------

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/css/<path:filename>')
def serve_css(filename):
    return send_from_directory(os.path.join(app.static_folder, 'css'), filename)


@app.route('/js/<path:filename>')
def serve_js(filename):
    return send_from_directory(os.path.join(app.static_folder, 'js'), filename)


@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory(os.path.join(app.static_folder, 'assets'), filename)


# --------------- Health & Status ---------------

@app.route('/api/health')
def health():
    seekdb_live = isinstance(material_client, SeekDBClient) and material_client.health_check()
    pc_live = isinstance(memory_client, PowerContextClient) and memory_client.health_check()
    return jsonify({
        'status': 'ok',
        'powercontext': pc_live,
        'seekdb': seekdb_live,
        'mode': 'full' if (seekdb_live and pc_live) else 'degraded',
        'seekdb_backend': 'seekdb' if seekdb_live else 'memory-fallback',
        'powercontext_backend': 'powercontext' if pc_live else 'memory-fallback',
        'material_count': material_client.count(),
        'timestamp': datetime.now().isoformat(),
    })


# --------------- Study Material APIs ---------------

@app.route('/api/materials', methods=['POST'])
def add_material():
    """Add a study material (note, paper summary, concept, etc.)."""
    data = request.json
    required = ['title', 'content', 'subject', 'topic']
    if not all(k in data for k in required):
        return jsonify({'error': f'Missing fields: {required}'}), 400

    result = material_client.add_material(
        title=data['title'],
        content=data['content'],
        subject=data['subject'],
        topic=data['topic'],
        material_type=data.get('type', 'note'),
        tags=data.get('tags', []),
    )

    # Also save to PowerContext as a memory
    memory_client.save_memory(
        content=f'Study material: {data["title"]} ({data["subject"]} > {data["topic"]})',
        memory_type='topic',
        metadata={
            'subject': data['subject'],
            'topic': data['topic'],
            'material_id': result.get('id'),
            'title': data['title'],
        }
    )

    return jsonify(result), 201


@app.route('/api/materials/search', methods=['GET'])
def search_materials():
    """Search study materials with hybrid retrieval."""
    query = request.args.get('q', '')
    subject = request.args.get('subject', None)
    limit = int(request.args.get('limit', 5))
    search_type = request.args.get('type', 'hybrid')

    if not query:
        return jsonify({'error': 'Query parameter q is required'}), 400

    results = material_client.search(query, subject=subject, limit=limit, search_type=search_type)

    # Log the search in PowerContext
    memory_client.save_memory(
        content=f'Search: "{query}" in {subject or "all subjects"}, found {len(results)} results',
        memory_type='insight',
        metadata={'query': query, 'subject': subject, 'result_count': len(results)}
    )

    return jsonify({
        'query': query,
        'subject': subject,
        'count': len(results),
        'results': results,
    })


@app.route('/api/materials', methods=['GET'])
def list_materials():
    """List all materials, optionally filtered by subject."""
    subject = request.args.get('subject', None)
    materials = material_client.list_materials(subject)
    return jsonify({'materials': materials, 'count': len(materials)})


# --------------- Learning Progress APIs ---------------

@app.route('/api/progress', methods=['POST'])
def update_progress():
    """Update learning progress for a topic."""
    data = request.json
    required = ['subject', 'topic', 'progress']
    if not all(k in data for k in required):
        return jsonify({'error': f'Missing fields: {required}'}), 400

    result = memory_client.save_learning_progress(
        subject=data['subject'],
        topic=data['topic'],
        progress_pct=data['progress'],
        notes=data.get('notes', ''),
    )
    return jsonify(result or {'status': 'saved'}), 201


@app.route('/api/progress/summary', methods=['GET'])
def progress_summary():
    """Get learning progress summary, optionally for a specific subject."""
    subject = request.args.get('subject', None)
    summary = memory_client.get_learning_summary(subject)
    return jsonify(summary)


# --------------- Weak Points APIs ---------------

@app.route('/api/weak-points', methods=['POST'])
def add_weak_point():
    """Record a weak knowledge point."""
    data = request.json
    required = ['subject', 'topic', 'description']
    if not all(k in data for k in required):
        return jsonify({'error': f'Missing fields: {required}'}), 400

    result = memory_client.save_weak_point(
        subject=data['subject'],
        topic=data['topic'],
        description=data['description'],
    )
    return jsonify(result or {'status': 'saved'}), 201


# --------------- Recommendations APIs ---------------

@app.route('/api/recommendations', methods=['GET'])
def get_recommendations():
    """Get personalized study recommendations based on learning history."""
    subject = request.args.get('subject', None)

    # Recall relevant memories
    query = f'study progress weak points {subject or ""}'
    memories = memory_client.recall_memories(query, limit=20)

    # Search for related materials
    search_query = subject or 'study'
    materials = material_client.search(search_query, subject=subject, limit=5)

    # Build recommendations
    recommendations = {
        'based_on_memories': len(memories) if isinstance(memories, list) else 0,
        'suggested_materials': materials[:3],
        'focus_areas': [],
        'next_steps': [],
    }

    # Extract weak points from memories
    if isinstance(memories, list):
        for m in memories:
            content = m.get('content', '') if isinstance(m, dict) else str(m)
            if 'weak point' in content.lower():
                recommendations['focus_areas'].append(content)
            elif 'progress' in content.lower() and '%' in content:
                recommendations['next_steps'].append(content)

    # Save recommendation event
    memory_client.save_experience(
        situation=f'User requested recommendations for {subject or "general study"}',
        action='Generated personalized recommendations',
        outcome=f'Found {len(materials)} related materials',
        lesson=f'User is focusing on {subject or "multiple subjects"}',
    )

    return jsonify(recommendations)


# --------------- Quiz APIs ---------------

@app.route('/api/quiz/generate', methods=['POST'])
def generate_quiz():
    """Generate a quiz from study materials."""
    data = request.json
    subject = data.get('subject', '')
    topic = data.get('topic', '')
    count = data.get('count', 5)

    # Search for relevant materials
    query = f'{subject} {topic}'
    materials = material_client.search(query, subject=subject or None, limit=count * 2)

    if not materials:
        return jsonify({'error': 'No materials found for quiz generation', 'quiz': None}), 404

    # Build quiz from materials
    quiz = {
        'subject': subject,
        'topic': topic,
        'generated_at': datetime.now().isoformat(),
        'questions': [],
    }

    for i, mat in enumerate(materials[:count]):
        content = mat.get('content', '')
        meta = mat.get('metadata', {})
        quiz['questions'].append({
            'id': i + 1,
            'type': 'open_ended',
            'prompt': f'Explain the key concepts from: {content[:200]}...',
            'source_material': meta.get('title', 'Unknown'),
            'reference_content': content[:500],
        })

    # Save quiz generation event
    memory_client.save_experience(
        situation=f'Quiz requested for {subject} > {topic}',
        action=f'Generated {len(quiz["questions"])} questions',
        outcome='Quiz created from study materials',
        lesson=f'User is testing knowledge on {topic}',
    )

    return jsonify(quiz)


# --------------- Study Session APIs ---------------

@app.route('/api/session/start', methods=['POST'])
def start_session():
    """Start a new study session."""
    data = request.json
    subject = data.get('subject', 'General')
    goal = data.get('goal', '')

    # Recall previous context
    context = memory_client.get_prepared_context(f'study {subject} {goal}')

    # Create handoff from previous session
    if context:
        memory_client.create_handoff(
            work_description=f'Starting study session: {subject}',
            next_steps=[goal] if goal else ['Review weak points', 'Continue progress'],
        )

    return jsonify({
        'session_id': datetime.now().strftime('%Y%m%d_%H%M%S'),
        'subject': subject,
        'goal': goal,
        'context': context,
        'started_at': datetime.now().isoformat(),
    })


@app.route('/api/session/end', methods=['POST'])
def end_session():
    """End a study session and save summary."""
    data = request.json
    session_id = data.get('session_id', '')
    summary = data.get('summary', '')
    topics_covered = data.get('topics_covered', [])
    time_spent = data.get('time_spent_minutes', 0)

    # Save session summary as memory
    memory_client.save_memory(
        content=f'Session {session_id}: {summary}. Topics: {", ".join(topics_covered)}. Time: {time_spent}min',
        memory_type='topic',
        metadata={
            'session_id': session_id,
            'topics': topics_covered,
            'time_spent': time_spent,
        }
    )

    # Save as experience
    memory_client.save_experience(
        situation=f'Study session {session_id} completed',
        action=f'Studied {len(topics_covered)} topics for {time_spent} minutes',
        outcome=summary,
        lesson=f'Covered: {", ".join(topics_covered)}',
    )

    return jsonify({'status': 'session_saved', 'session_id': session_id})


# --------------- Chat / Ask APIs ---------------

@app.route('/api/ask', methods=['POST'])
def ask_question():
    """Ask a study question - gets context from PowerContext + seekdb."""
    data = request.json
    question = data.get('question', '')
    subject = data.get('subject', None)

    if not question:
        return jsonify({'error': 'Question is required'}), 400

    # 1. Recall relevant memories from PowerContext
    memories = memory_client.recall_memories(question, limit=10)

    # 2. Search study materials from seekdb
    materials = material_client.search(question, subject=subject, limit=5)

    # 3. Get prepared context
    prepared = memory_client.get_prepared_context(question)

    # 4. Build response with evidence
    response = {
        'question': question,
        'context_sources': {
            'memories': memories if isinstance(memories, list) else [],
            'materials': materials,
            'prepared_context': prepared,
        },
        'answer': _build_answer(question, memories, materials),
        'related_topics': _extract_topics(memories, materials),
    }

    # 5. Log the Q&A
    memory_client.save_experience(
        situation=f'User asked: {question[:100]}',
        action='Searched memories and materials',
        outcome=f'Found {len(memories) if isinstance(memories, list) else 0} memories, {len(materials)} materials',
        lesson=response['answer'][:200],
    )

    return jsonify(response)


def _build_answer(question, memories, materials):
    """Build an answer from available context."""
    parts = []

    if materials:
        parts.append('Based on your study materials:\n')
        for i, mat in enumerate(materials[:3]):
            content = mat.get('content', '')[:300]
            meta = mat.get('metadata', {})
            title = meta.get('title', 'Unknown')
            parts.append(f'  [{i+1}] From "{title}": {content}...\n')

    if isinstance(memories, list) and memories:
        parts.append('\nFrom your learning history:\n')
        for mem in memories[:3]:
            content = mem.get('content', '') if isinstance(mem, dict) else str(mem)
            parts.append(f'  - {content[:200]}\n')

    if not parts:
        return 'No relevant study materials or learning history found. Try adding some notes first!'

    return '\n'.join(parts)


def _extract_topics(memories, materials):
    """Extract related topics from search results."""
    topics = set()
    if isinstance(memories, list):
        for m in memories:
            meta = m.get('metadata', {}) if isinstance(m, dict) else {}
            if 'subject' in meta:
                topics.add(meta['subject'])
            if 'topic' in meta:
                topics.add(meta['topic'])
    for mat in materials:
        meta = mat.get('metadata', {})
        if 'subject' in meta:
            topics.add(meta['subject'])
        if 'topic' in meta:
            topics.add(meta['topic'])
    return list(topics)


# --------------- Sample Data Loader ---------------

@app.route('/api/load-samples', methods=['POST'])
def load_sample_data():
    """Load sample study materials for demo purposes."""
    samples = [
        {
            'title': 'Python 列表推导式',
            'content': '列表推导式是 Python 中一种简洁的列表创建方式。语法为 [expression for item in iterable if condition]。例如：[x**2 for x in range(10) if x % 2 == 0] 生成偶数的平方列表 [0, 4, 16, 36, 64]。列表推导式比等价的 for 循环更简洁，通常也更快。',
            'subject': 'Python编程',
            'topic': '列表与数据结构',
            'type': 'concept',
            'tags': ['python', '列表', '推导式'],
        },
        {
            'title': '机器学习 - 监督学习概述',
            'content': '监督学习是机器学习的一种方法，使用标注数据训练模型。训练数据包含输入特征和对应的正确输出（标签）。常见任务包括分类（预测离散标签）和回归（预测连续值）。算法包括线性回归、决策树、SVM、神经网络等。评估指标有准确率、精确率、召回率、F1分数等。',
            'subject': '机器学习',
            'topic': '监督学习基础',
            'type': 'note',
            'tags': ['ML', '监督学习', '分类', '回归'],
        },
        {
            'title': '数据库索引原理',
            'content': '数据库索引是一种数据结构，用于加速查询。常见索引类型：B+树索引（适合范围查询）、哈希索引（适合等值查询）、全文索引（适合文本搜索）。索引通过减少磁盘I/O提高查询速度，但会增加写入开销和存储空间。选择合适的索引需要分析查询模式。',
            'subject': '数据库系统',
            'topic': '索引与查询优化',
            'type': 'concept',
            'tags': ['数据库', '索引', 'B+树', '性能优化'],
        },
        {
            'title': 'HTTP 状态码总结',
            'content': '2xx 成功：200 OK, 201 Created, 204 No Content。3xx 重定向：301 永久重定向, 302 临时重定向, 304 Not Modified。4xx 客户端错误：400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found。5xx 服务端错误：500 Internal Server Error, 502 Bad Gateway, 503 Service Unavailable。',
            'subject': '计算机网络',
            'topic': 'HTTP协议',
            'type': 'note',
            'tags': ['HTTP', '网络', '状态码'],
        },
        {
            'title': '向量检索与语义搜索',
            'content': '向量检索通过将文本转换为高维向量，利用向量间的距离（如余弦相似度）衡量语义相关性。与传统关键词搜索不同，语义搜索能理解同义词和上下文含义。典型流程：1) 使用 Embedding 模型将文档向量化 2) 存入向量数据库 3) 查询时计算相似度排序。应用场景包括 RAG、推荐系统等。',
            'subject': '数据库系统',
            'topic': '向量数据库',
            'type': 'concept',
            'tags': ['向量检索', '语义搜索', 'Embedding', 'RAG'],
        },
    ]

    results = []
    for s in samples:
        result = material_client.add_material(
            title=s['title'],
            content=s['content'],
            subject=s['subject'],
            topic=s['topic'],
            material_type=s.get('type', 'note'),
            tags=s.get('tags', []),
        )
        memory_client.save_memory(
            content=f'Sample material loaded: {s["title"]} ({s["subject"]})',
            memory_type='topic',
            metadata={'subject': s['subject'], 'topic': s['topic'], 'title': s['title']}
        )
        results.append(result)

    return jsonify({
        'status': 'loaded',
        'count': len(results),
        'materials': results,
    })


if __name__ == '__main__':
    logger.info(f'Starting StudyMind backend on {Config.FLASK_HOST}:{Config.FLASK_PORT}')
    logger.info(f'PowerContext: {Config.POWERCONTEXT_API_URL}')
    logger.info(f'seekdb: {Config.SEEKDB_HOST}:{Config.SEEKDB_PORT}')
    app.run(
        host=Config.FLASK_HOST,
        port=Config.FLASK_PORT,
        debug=Config.FLASK_DEBUG,
    )
