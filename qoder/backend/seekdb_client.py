"""
seekdb Client - study material storage and hybrid retrieval.

Responsibility split with PowerContext:
- seekdb owns "knowledge": material text, vectors, full-text index, structured filters
- PowerContext owns "memory": learning progress, weak points, retrieval behaviour

Implementation notes:
- Connects to the seekdb server over the MySQL protocol via pyseekdb.
- Embeddings are produced locally by the bundled all-MiniLM-L6-v2 ONNX model
  (384 dims), so no external API key is required to run the project.
- A single query can run vector + IK full-text together (search_mode="hybrid")
  and still apply structured filters such as subject.
"""

import os
import sys
import re
import json
import socket
import hashlib
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _register_windows_dll_dir():
    """onnxruntime ships msvcp140.dll next to the venv scripts on Windows."""
    if os.name != 'nt':
        return
    for candidate in (
        os.path.join(sys.prefix, 'Scripts'),
        os.path.join(os.path.dirname(sys.executable), 'Scripts'),
    ):
        if os.path.isdir(candidate):
            try:
                os.add_dll_directory(candidate)
            except (AttributeError, OSError):
                pass


_register_windows_dll_dir()

try:
    import pyseekdb
    from pyseekdb import Schema, IVFConfiguration, FulltextIndexConfig
    SEEKDB_AVAILABLE = True
    SEEKDB_IMPORT_ERROR = None
except Exception as exc:  # ImportError or onnxruntime DLL failure
    SEEKDB_AVAILABLE = False
    SEEKDB_IMPORT_ERROR = str(exc)
    logger.warning('pyseekdb not available, using fallback store: %s', exc)


def _ensure_database(host, port, user, password, database):
    """seekdb will not create the database implicitly; do it before connecting."""
    try:
        import pymysql
    except ImportError:
        return
    try:
        conn = pymysql.connect(host=host, port=port, user=user,
                               password=password or '', connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                "DEFAULT CHARACTER SET utf8mb4"
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning('could not ensure database %s: %s', database, exc)


def _port_open(host, port, timeout=1.5):
    """Cheap reachability probe.

    Connecting through pyseekdb to a dead port costs ~15s of retries, which is
    enough to make an automated health check time out. Bail out early instead.
    """
    if not host:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class SeekDBClient:
    """seekdb-backed study material store with vector + full-text retrieval."""

    COLLECTION_NAME = 'study_materials'
    EMBEDDING_DIM = 384          # all-MiniLM-L6-v2
    ANALYZER = 'ik'              # IK full-text analyzer bundled with seekdb

    def __init__(self, host=None, port=None, user=None, password=None, database=None,
                 path=None):
        self.host = host or ''
        self.port = port or 2881
        self.user = user or 'root'
        self.password = password or ''
        self.database = database or 'study_mind'
        self.path = path or os.getenv('SEEKDB_PATH') or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'seekdb')
        self.backend = None          # 'embedded' | 'server'
        self.client = None
        self.collection = None
        self.embedding_function = None
        self._init_client()

    # ---------- lifecycle ----------

    def _setup_collection(self):
        self.embedding_function = pyseekdb.DefaultEmbeddingFunction()
        self.collection = self.client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            schema=Schema(
                vector_index=IVFConfiguration(
                    dimension=self.EMBEDDING_DIM,
                    distance='cosine',
                ),
                fulltext_index=FulltextIndexConfig(analyzer=self.ANALYZER),
            ),
            embedding_function=self.embedding_function,
        )

    def _connect_embedded(self):
        """Embedded seekdb: a local on-disk database, no server or port needed.

        Requires pylibseekdb, which currently ships Linux wheels only, so this
        path is skipped on Windows/macOS and the remote path is used instead.
        """
        try:
            os.makedirs(self.path, exist_ok=True)
            self.client = pyseekdb.Client(path=self.path, database=self.database)
            self._setup_collection()
            self.backend = 'embedded'
            logger.info('seekdb ready (embedded): %s collection=%s',
                        self.path, self.COLLECTION_NAME)
            return True
        except Exception as exc:
            logger.info('embedded seekdb not available (%s)', str(exc)[:140])
            self.client = None
            self.collection = None
            return False

    def _connect_server(self):
        """seekdb over the MySQL protocol (a running seekdb server)."""
        if not _port_open(self.host, self.port):
            logger.info('seekdb server %s:%s not listening', self.host, self.port)
            return False
        _ensure_database(self.host, self.port, self.user, self.password, self.database)
        try:
            self.client = pyseekdb.Client(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
            )
            self._setup_collection()
            self.backend = 'server'
            logger.info('seekdb ready (server): %s:%s/%s collection=%s',
                        self.host, self.port, self.database, self.COLLECTION_NAME)
            return True
        except Exception as exc:
            logger.info('seekdb server not reachable (%s)', str(exc)[:140])
            self.client = None
            self.collection = None
            return False

    def _init_client(self):
        if not SEEKDB_AVAILABLE:
            return
        # An explicit SEEKDB_HOST means "use the server I configured".
        # Otherwise prefer the embedded engine so the project runs on a bare
        # machine with nothing else installed, then fall back to localhost.
        if self.host:
            if self._connect_server():
                return
            self._connect_embedded()
            return
        if self._connect_embedded():
            return
        self.host = 'localhost'
        self._connect_server()

    def health_check(self):
        try:
            if self.collection is None:
                return False
            self.collection.count()
            return True
        except Exception as exc:
            logger.warning('seekdb health check failed: %s', exc)
            return False

    # ---------- writes ----------

    def add_material(self, title, content, subject, topic, material_type='note', tags=None):
        """Persist one study material; seekdb indexes both vector and full text."""
        doc_id = hashlib.md5(f'{title}_{content[:50]}'.encode('utf-8')).hexdigest()[:16]
        metadata = {
            'title': title,
            'subject': subject,
            'topic': topic,
            'type': material_type,
            'tags': json.dumps(tags or [], ensure_ascii=False),
            'created_at': datetime.now().isoformat(timespec='seconds'),
        }

        if self.collection is not None:
            try:
                self.collection.add(
                    ids=[doc_id],
                    documents=[content],
                    metadatas=[metadata],
                )
                logger.info('seekdb add: id=%s subject=%s', doc_id, subject)
                return {'id': doc_id, 'status': 'stored_in_seekdb', 'metadata': metadata}
            except Exception as exc:
                logger.error('seekdb add failed: %s', exc)

        return {
            'id': doc_id,
            'status': 'fallback',
            'metadata': metadata,
            'content': content,
        }

    def delete_material(self, doc_id):
        if self.collection is None:
            return {'status': 'unavailable'}
        self.collection.delete(ids=[doc_id])
        logger.info('seekdb delete: id=%s', doc_id)
        return {'status': 'deleted', 'id': doc_id}

    # ---------- reads ----------

    def search(self, query, subject=None, limit=5, search_type='hybrid'):
        """Retrieval over seekdb.

        hybrid  : one statement combining vector KNN + IK full-text + metadata
                  filter, fused with RRF (this is seekdb's Hybrid Search).
        vector  : vector similarity only.
        fulltext: IK keyword match only (where_document).
        """
        if self.collection is None:
            return []

        where = {'subject': subject} if subject else None
        include = ['documents', 'metadatas', 'distances']

        try:
            if search_type == 'hybrid':
                ft_query = {'where_document': {'$contains': query}, 'n_results': limit}
                knn_query = {'query_texts': [query], 'n_results': limit}
                if where:
                    ft_query['where'] = where
                    knn_query['where'] = where
                raw = self.collection.hybrid_search(
                    query=ft_query,
                    knn=knn_query,
                    rank={'rrf': {'rank_window_size': 60, 'rank_constant': 60}},
                    n_results=limit,
                    include=include,
                )
                logger.info('seekdb hybrid_search: q=%r subject=%s hits=%d',
                            query, subject, len((raw.get('ids') or [[]])[0] or []))
            elif search_type == 'fulltext':
                payload = {
                    'where_document': {'$contains': query},
                    'n_results': limit,
                    'include': include,
                }
                if where:
                    payload['where'] = where
                raw = self.collection.query(**payload)
            else:
                payload = {'query_texts': [query], 'n_results': limit, 'include': include}
                if where:
                    payload['where'] = where
                raw = self.collection.query(**payload)
        except Exception as exc:
            logger.warning('seekdb %s search failed (%s), falling back to vector only',
                           search_type, exc)
            try:
                payload = {'query_texts': [query], 'n_results': limit, 'include': include}
                if where:
                    payload['where'] = where
                raw = self.collection.query(**payload)
            except Exception as inner:
                logger.error('seekdb search failed: %s', inner)
                return []

        return self._format_results(raw)

    def list_materials(self, subject=None, limit=100):
        if self.collection is None:
            return []
        payload = {'limit': limit, 'include': ['documents', 'metadatas']}
        if subject:
            payload['where'] = {'subject': subject}
        try:
            raw = self.collection.get(**payload)
        except Exception as exc:
            logger.error('seekdb list failed: %s', exc)
            return []
        return self._format_results(raw, score=None)

    def count(self):
        if self.collection is None:
            return 0
        try:
            return self.collection.count()
        except Exception:
            return 0

    @staticmethod
    def _format_results(raw, score=0.0):
        formatted = []
        if not raw:
            return formatted
        docs = (raw.get('documents') or [[]])
        docs = docs[0] if docs and isinstance(docs[0], list) else docs
        metas = (raw.get('metadatas') or [[]])
        metas = metas[0] if metas and isinstance(metas[0], list) else metas
        ids = (raw.get('ids') or [[]])
        ids = ids[0] if ids and isinstance(ids[0], list) else ids
        dists = (raw.get('distances') or [[]])
        dists = dists[0] if dists and isinstance(dists[0], list) else dists

        for i, doc in enumerate(docs):
            meta = metas[i] if i < len(metas) and metas[i] else {}
            dist = dists[i] if i < len(dists) else None
            formatted.append({
                'id': ids[i] if i < len(ids) else f'doc_{i}',
                'content': doc,
                'metadata': meta,
                'score': round(1 - dist, 4) if isinstance(dist, (int, float)) else score,
            })
        return formatted


class FallbackMaterialStore:
    """In-memory store used only when seekdb cannot be reached."""

    def __init__(self):
        self.materials = []
        self._id_counter = 0

    def add_material(self, title, content, subject, topic, material_type='note', tags=None):
        self._id_counter += 1
        doc_id = f'mat_{self._id_counter}'
        material = {
            'id': doc_id,
            'title': title,
            'content': content,
            'subject': subject,
            'topic': topic,
            'type': material_type,
            'tags': tags or [],
            'created_at': datetime.now().isoformat(timespec='seconds'),
        }
        self.materials.append(material)
        return {'id': doc_id, 'status': 'stored_in_memory', 'metadata': material}

    @staticmethod
    def _tokens(text):
        """Tokenise text for scoring: latin words plus Chinese chars/bigrams.

        Chinese queries contain no spaces, so splitting on whitespace alone
        never matches anything - which silently returned zero results, and
        therefore an empty quiz, whenever seekdb was unavailable.
        """
        text = (text or '').lower()
        tokens = set()
        for word in re.findall(r'[a-z0-9+_.#-]{2,}', text):
            tokens.add(word)
        for seg in re.findall(r'[\u4e00-\u9fff]+', text):
            if len(seg) == 1:
                tokens.add(seg)
            for i in range(len(seg) - 1):
                tokens.add(seg[i:i + 2])
        return tokens

    def search(self, query, subject=None, limit=5, search_type='hybrid'):
        results = self.materials
        if subject:
            results = [m for m in results if m.get('subject') == subject]
        q_tokens = self._tokens(query)
        if not q_tokens:
            return [
                {'id': m['id'], 'content': m['content'], 'metadata': m, 'score': 0.0}
                for m in results[:limit]
            ]
        scored = []
        for m in results:
            text = f"{m.get('title', '')} {m.get('content', '')}"
            hits = len(q_tokens & self._tokens(text))
            if hits:
                scored.append((hits / len(q_tokens), m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {'id': m['id'], 'content': m['content'], 'metadata': m, 'score': round(s, 4)}
            for s, m in scored[:limit]
        ]

    def list_materials(self, subject=None, limit=100):
        items = self.materials
        if subject:
            items = [m for m in items if m.get('subject') == subject]
        return [{'id': m['id'], 'content': m['content'], 'metadata': m, 'score': 0.0}
                for m in items[:limit]]

    def get_all_materials(self, subject=None):
        return self.list_materials(subject)

    def count(self):
        return len(self.materials)

    def health_check(self):
        return True
