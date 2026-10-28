"""
Database initialization script.
Sets up seekdb collection and loads sample data.
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from seekdb_client import SeekDBClient, FallbackMaterialStore
from powercontext_client import PowerContextClient, FallbackMemoryStore


def init_seekdb():
    """Initialize seekdb collection and load sample data."""
    print('Initializing seekdb...')
    try:
        client = SeekDBClient()
        if not client.health_check():
            print('  seekdb not available, using fallback store')
            client = FallbackMaterialStore()
    except Exception as e:
        print(f'  seekdb init failed: {e}, using fallback store')
        client = FallbackMaterialStore()

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

    count = 0
    for s in samples:
        result = client.add_material(**s)
        count += 1
        print(f'  Added: {s["title"]} -> {result.get("status", "unknown")}')

    print(f'  Total: {count} materials loaded')
    return count


def init_powercontext():
    """Initialize PowerContext with sample learning data."""
    print('Initializing PowerContext...')
    try:
        client = PowerContextClient()
        if not client.health_check():
            print('  PowerContext not available, using fallback store')
            client = FallbackMemoryStore()
    except Exception as e:
        print(f'  PowerContext init failed: {e}, using fallback store')
        client = FallbackMemoryStore()

    client.save_learning_progress('Python编程', '列表推导式', 75, '基本掌握，需要更多练习')
    client.save_learning_progress('数据库系统', '索引原理', 60, '理解B+树，需要学习Hash索引')
    client.save_weak_point('机器学习', '反向传播', '梯度消失和梯度爆炸问题理解不深')
    client.save_preference('study_style', '喜欢通过实例学习概念')

    print('  Sample learning data saved')


if __name__ == '__main__':
    print('=== StudyMind Database Initialization ===\n')
    init_seekdb()
    print()
    init_powercontext()
    print('\n=== Initialization Complete ===')
