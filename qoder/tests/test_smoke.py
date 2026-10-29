"""Smoke tests for the StudyMind backend.

They run with the minimal dependency set only (Flask), so a reviewer can verify
the project end to end without starting seekdb or PowerContext:

    pip install -r requirements.txt
    python -m pytest -q
"""

import importlib.util
import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend')
sys.path.insert(0, BACKEND)

_spec = importlib.util.spec_from_file_location(
    'studymind_backend', os.path.join(BACKEND, 'app.py')
)
_backend = importlib.util.module_from_spec(_spec)
sys.modules['studymind_backend'] = _backend
_spec.loader.exec_module(_backend)

app = _backend.app
_backend._ensure_seed_data()


def client():
    return app.test_client()


def test_health_reports_backend_mode():
    data = client().get('/api/health').get_json()
    assert data['status'] == 'ok'
    assert data['mode'] in ('full', 'degraded')
    assert data['material_count'] >= 1


def test_index_page_served():
    resp = client().get('/')
    assert resp.status_code == 200
    assert b'StudyMind' in resp.data


def test_materials_seeded_and_listed():
    data = client().get('/api/materials').get_json()
    assert data['count'] >= 5


def test_hybrid_search_returns_hit():
    data = client().get('/api/materials/search?q=%E5%90%91%E9%87%8F%E6%A3%80%E7%B4%A2&limit=3').get_json()
    assert data['count'] >= 1
    assert data['results'][0]['metadata']['title']


def test_subject_filter_is_applied():
    data = client().get(
        '/api/materials/search?q=%E7%B4%A2%E5%BC%95&subject=%E6%95%B0%E6%8D%AE%E5%BA%93%E7%B3%BB%E7%BB%9F'
    ).get_json()
    assert data['count'] >= 1
    for item in data['results']:
        assert item['metadata']['subject'] == '数据库系统'


def test_quiz_generated_from_materials():
    data = client().post('/api/quiz/generate', json={'subject': '数据库系统', 'count': 2}).get_json()
    assert len(data['questions']) >= 1


def test_progress_write_is_recalled():
    before = client().get('/api/progress/summary').get_json()['total_memories']
    written = client().post(
        '/api/progress',
        json={'subject': '数据库系统', 'topic': '测试主题', 'progress': 42},
    ).get_json()
    assert written['artifact_id']
    after = client().get('/api/progress/summary').get_json()['total_memories']
    assert after == before + 1


def test_recommendations_are_not_empty():
    data = client().get('/api/recommendations').get_json()
    assert data['suggested_materials'], 'recommendations must suggest materials'
    assert data['based_on_memories'] >= 1


def test_ask_returns_answer_with_sources():
    data = client().post('/api/ask', json={'question': '向量索引和B+树索引有什么区别'}).get_json()
    assert data['answer']
    assert data['context_sources']['materials']
