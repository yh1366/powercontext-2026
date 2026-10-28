"""
seekdb Client - Handles study material storage and hybrid retrieval.

seekdb provides:
- Vector search for semantic similarity
- Full-text search for keyword matching
- Hybrid search combining both
- Structured filtering with SQL
"""

import json
import logging
import hashlib
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import pyseekdb
    from pyseekdb import Schema, VectorIndexConfig, IVFConfiguration, FulltextIndexConfig
    SEEKDB_AVAILABLE = True
except ImportError:
    SEEKDB_AVAILABLE = False
    logger.warning('pyseekdb not installed, using fallback store')

try:
    from openai import OpenAI
    from config import Config
    OPENAI_AVAILABLE = bool(Config.OPENAI_API_KEY and Config.OPENAI_API_KEY != 'your_openai_api_key_here')
except Exception:
    OPENAI_AVAILABLE = False


def _get_embedding(text, model=None):
    """Get embedding vector for text using OpenAI API."""
    if not OPENAI_AVAILABLE:
        return None
    try:
        client = OpenAI(api_key=Config.OPENAI_API_KEY, base_url=Config.OPENAI_BASE_URL)
        resp = client.embeddings.create(
            input=text,
            model=model or Config.OPENAI_EMBEDDING_MODEL
        )
        return resp.data[0].embedding
    except Exception as e:
        logger.error(f'Embedding failed: {e}')
        return None


class SeekDBClient:
    """Client for seekdb study material storage and retrieval."""

    COLLECTION_NAME = 'study_materials'
    EMBEDDING_DIM = 1536

    def __init__(self, host=None, port=None, user=None, password=None, database=None):
        self.host = host or 'localhost'
        self.port = port or 2881
        self.user = user or 'root'
        self.password = password or ''
        self.database = database or 'study_mind'
        self.client = None
        self.collection = None
        self._init_client()

    def _init_client(self):
        if not SEEKDB_AVAILABLE:
            return
        try:
            self.client = pyseekdb.Client(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
            )
            self.collection = self.client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                schema=Schema(
                    vector_index=VectorIndexConfig(
                        ivf=IVFConfiguration(
                            dimension=self.EMBEDDING_DIM,
                            distance='cosine',
                        )
                    ),
                    fulltext_index=FulltextIndexConfig(analyzer='ik'),
                ),
                use_namespace=True,
            )
            logger.info('seekdb client initialized successfully')
        except Exception as e:
            logger.warning(f'seekdb init failed: {e}, will use fallback')
            self.client = None

    def add_material(self, title, content, subject, topic, material_type='note', tags=None):
        """Add a study material to the knowledge base."""
        doc_id = hashlib.md5(f'{title}_{content[:50]}'.encode()).hexdigest()[:16]
        embedding = _get_embedding(f'{title}\n{content[:500]}')

        metadata = {
            'subject': subject,
            'topic': topic,
            'type': material_type,
            'tags': json.dumps(tags or []),
            'created_at': datetime.now().isoformat(),
            'title': title,
        }

        if self.collection and embedding:
            try:
                ns = self.collection.get_or_create_namespace('default')
                ns.add(
                    ids=[doc_id],
                    documents=[content],
                    metadatas=[metadata],
                    embeddings=[embedding],
                )
                logger.info(f'Material added to seekdb: {doc_id}')
                return {'id': doc_id, 'status': 'stored_in_seekdb'}
            except Exception as e:
                logger.error(f'seekdb add failed: {e}')

        return {'id': doc_id, 'status': 'fallback', 'metadata': metadata, 'content': content}

    def search(self, query, subject=None, limit=5, search_type='hybrid'):
        """
        Search study materials.
        
        Args:
            query: Search text
            subject: Optional subject filter
            limit: Max results
            search_type: 'vector', 'fulltext', or 'hybrid'
        """
        if self.collection:
            try:
                ns = self.collection.get_or_create_namespace('default')
                embedding = _get_embedding(query)

                where = {'subject': subject} if subject else None

                if search_type == 'hybrid' and embedding:
                    results = ns.query(
                        query_texts=[query],
                        query_embeddings=[embedding],
                        n_results=limit,
                        where=where,
                        search_mode='hybrid',
                    )
                elif search_type == 'vector' and embedding:
                    results = ns.query(
                        query_embeddings=[embedding],
                        n_results=limit,
                        where=where,
                    )
                else:
                    results = ns.query(
                        query_texts=[query],
                        n_results=limit,
                        where=where,
                    )
                return self._format_results(results)
            except Exception as e:
                logger.error(f'seekdb search failed: {e}')

        return self._fallback_search(query, subject, limit)

    def _format_results(self, results):
        formatted = []
        if results and hasattr(results, 'documents'):
            docs = results.documents[0] if results.documents else []
            metas = results.metadatas[0] if results.metadatas else []
            dists = results.distances[0] if hasattr(results, 'distances') and results.distances else []
            for i, doc in enumerate(docs):
                meta = metas[i] if i < len(metas) else {}
                dist = dists[i] if i < len(dists) else None
                formatted.append({
                    'content': doc,
                    'metadata': meta,
                    'score': dist,
                })
        return formatted

    def _fallback_search(self, query, subject, limit):
        return []

    def health_check(self):
        return self.client is not None


class FallbackMaterialStore:
    """In-memory fallback when seekdb is unavailable."""

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
            'created_at': datetime.now().isoformat(),
        }
        self.materials.append(material)
        return {'id': doc_id, 'status': 'stored_in_memory'}

    def search(self, query, subject=None, limit=5, search_type='hybrid'):
        results = self.materials
        if subject:
            results = [m for m in results if m.get('subject') == subject]

        query_lower = query.lower()
        scored = []
        for m in results:
            score = 0
            text = f"{m.get('title', '')} {m.get('content', '')}".lower()
            for w in query_lower.split():
                if w in text:
                    score += 1
            scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {'content': m['content'], 'metadata': m, 'score': s}
            for s, m in scored[:limit]
            if s > 0
        ]

    def health_check(self):
        return True

    def get_all_materials(self, subject=None):
        if subject:
            return [m for m in self.materials if m.get('subject') == subject]
        return list(self.materials)
