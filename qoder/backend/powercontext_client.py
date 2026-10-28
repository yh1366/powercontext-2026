"""
PowerContext Client - Handles memory and context management for the learning assistant.

PowerContext provides:
- Memory: Store and recall learning decisions, progress, and constraints
- Experience: Record learning situations, actions, and outcomes
- Scope: Isolate context per user or study session
- Handoff: Maintain continuity between study sessions
"""

import requests
import json
import logging
from datetime import datetime
from config import Config

logger = logging.getLogger(__name__)


class PowerContextClient:
    """Client for interacting with PowerContext HTTP API."""

    def __init__(self, base_url=None, scope_id=None):
        self.base_url = (base_url or Config.POWERCONTEXT_API_URL).rstrip('/')
        self.scope_id = scope_id or Config.POWERCONTEXT_SCOPE_ID
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'X-Scope-Id': self.scope_id,
        })

    def health_check(self):
        """Check if PowerContext server is running."""
        try:
            resp = self.session.get(f'{self.base_url}/api/v1/system/health', timeout=5)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f'PowerContext health check failed: {e}')
            return False

    def save_memory(self, content, memory_type='topic', metadata=None):
        """
        Save a learning memory (note, decision, insight).
        
        Args:
            content: The memory content text
            memory_type: One of 'topic', 'decision', 'insight', 'preference'
            metadata: Optional dict with extra info (subject, tags, etc.)
        """
        payload = {
            'content': content,
            'artifact_type': 'memory',
            'family': memory_type,
            'metadata': metadata or {},
            'source': {
                'type': 'journal',
                'entry': f'Learning entry at {datetime.now().isoformat()}',
            }
        }
        try:
            resp = self.session.post(
                f'{self.base_url}/api/v1/scopes/{self.scope_id}/memories',
                json=payload,
                timeout=10
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                logger.info(f'Memory saved: {data.get("artifact_id", "unknown")}')
                return data
            else:
                logger.error(f'Save memory failed: {resp.status_code} {resp.text}')
                return None
        except Exception as e:
            logger.error(f'Save memory exception: {e}')
            return None

    def recall_memories(self, query, memory_type=None, limit=10):
        """
        Recall relevant memories for a query.
        
        Args:
            query: Search text
            memory_type: Optional filter by type
            limit: Max results to return
        """
        params = {
            'query': query,
            'limit': limit,
        }
        if memory_type:
            params['family'] = memory_type

        try:
            resp = self.session.get(
                f'{self.base_url}/api/v1/scopes/{self.scope_id}/memories/recall',
                params=params,
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error(f'Recall failed: {resp.status_code} {resp.text}')
                return []
        except Exception as e:
            logger.error(f'Recall exception: {e}')
            return []

    def save_experience(self, situation, action, outcome, lesson):
        """
        Record a learning experience (situation-action-outcome-lesson).
        
        Args:
            situation: What happened
            action: What the learner did
            outcome: The result
            lesson: What was learned
        """
        payload = {
            'artifact_type': 'experience',
            'content': json.dumps({
                'situation': situation,
                'action': action,
                'outcome': outcome,
                'lesson': lesson,
                'timestamp': datetime.now().isoformat(),
            }),
            'metadata': {
                'subject': situation[:50],
                'tags': ['learning_experience'],
            }
        }
        try:
            resp = self.session.post(
                f'{self.base_url}/api/v1/scopes/{self.scope_id}/experiences',
                json=payload,
                timeout=10
            )
            if resp.status_code in (200, 201):
                return resp.json()
            else:
                logger.error(f'Save experience failed: {resp.status_code}')
                return None
        except Exception as e:
            logger.error(f'Save experience exception: {e}')
            return None

    def get_prepared_context(self, query, budget=4000):
        """
        Get a prepared context bundle for a query.
        
        Args:
            query: The current task/question
            budget: Max token budget for context
        """
        payload = {
            'query': query,
            'budget': budget,
        }
        try:
            resp = self.session.post(
                f'{self.base_url}/api/v1/scopes/{self.scope_id}/recall/prepared-context',
                json=payload,
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.warning(f'Prepared context failed: {resp.status_code}')
                return None
        except Exception as e:
            logger.warning(f'Prepared context exception: {e}')
            return None

    def save_learning_progress(self, subject, topic, progress_pct, notes=''):
        """Save learning progress for a specific topic."""
        return self.save_memory(
            content=f'Progress: {subject} > {topic} = {progress_pct}% complete. {notes}',
            memory_type='topic',
            metadata={
                'subject': subject,
                'topic': topic,
                'progress': progress_pct,
                'timestamp': datetime.now().isoformat(),
            }
        )

    def save_weak_point(self, subject, topic, description):
        """Record a weak knowledge point that needs more practice."""
        return self.save_memory(
            content=f'Weak point: {subject} > {topic} - {description}',
            memory_type='insight',
            metadata={
                'subject': subject,
                'topic': topic,
                'type': 'weak_point',
                'timestamp': datetime.now().isoformat(),
            }
        )

    def save_preference(self, key, value):
        """Save a learning preference."""
        return self.save_memory(
            content=f'Preference: {key} = {value}',
            memory_type='preference',
            metadata={
                'preference_key': key,
                'timestamp': datetime.now().isoformat(),
            }
        )

    def get_learning_summary(self, subject=None):
        """Get a summary of all learning memories for a subject."""
        query = f'learning progress {subject}' if subject else 'all learning progress'
        memories = self.recall_memories(query, limit=50)
        
        summary = {
            'total_memories': len(memories) if isinstance(memories, list) else 0,
            'memories': memories if isinstance(memories, list) else [],
            'subject': subject,
        }
        return summary

    def create_handoff(self, work_description, next_steps):
        """Create a handoff record for study session continuity."""
        payload = {
            'artifact_type': 'handoff',
            'content': json.dumps({
                'work_description': work_description,
                'next_steps': next_steps,
                'timestamp': datetime.now().isoformat(),
            }),
            'metadata': {
                'type': 'study_session_handoff',
            }
        }
        try:
            resp = self.session.post(
                f'{self.base_url}/api/v1/scopes/{self.scope_id}/handoffs',
                json=payload,
                timeout=10
            )
            if resp.status_code in (200, 201):
                return resp.json()
            return None
        except Exception as e:
            logger.error(f'Create handoff exception: {e}')
            return None

    def list_scopes(self):
        """List all available scopes."""
        try:
            resp = self.session.get(
                f'{self.base_url}/api/v1/scopes',
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception as e:
            logger.error(f'List scopes exception: {e}')
            return []


class FallbackMemoryStore:
    """
    Fallback in-memory store when PowerContext server is unavailable.
    Provides the same interface for basic operations.
    """

    def __init__(self):
        self.memories = []
        self.experiences = []
        self.handoffs = []
        self._id_counter = 0

    def _next_id(self):
        self._id_counter += 1
        return f'fallback_{self._id_counter}'

    def save_memory(self, content, memory_type='topic', metadata=None):
        mem = {
            'artifact_id': self._next_id(),
            'content': content,
            'family': memory_type,
            'metadata': metadata or {},
            'created_at': datetime.now().isoformat(),
        }
        self.memories.append(mem)
        return mem

    def recall_memories(self, query, memory_type=None, limit=10):
        results = self.memories
        if memory_type:
            results = [m for m in results if m.get('family') == memory_type]
        query_lower = query.lower()
        scored = []
        for m in results:
            score = sum(1 for w in query_lower.split() if w in m.get('content', '').lower())
            scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    def save_experience(self, situation, action, outcome, lesson):
        exp = {
            'artifact_id': self._next_id(),
            'situation': situation,
            'action': action,
            'outcome': outcome,
            'lesson': lesson,
            'created_at': datetime.now().isoformat(),
        }
        self.experiences.append(exp)
        return exp

    def save_learning_progress(self, subject, topic, progress_pct, notes=''):
        return self.save_memory(
            content=f'Progress: {subject} > {topic} = {progress_pct}% complete. {notes}',
            memory_type='topic',
            metadata={'subject': subject, 'topic': topic, 'progress': progress_pct}
        )

    def save_weak_point(self, subject, topic, description):
        return self.save_memory(
            content=f'Weak point: {subject} > {topic} - {description}',
            memory_type='insight',
            metadata={'subject': subject, 'topic': topic, 'type': 'weak_point'}
        )

    def save_preference(self, key, value):
        return self.save_memory(
            content=f'Preference: {key} = {value}',
            memory_type='preference',
            metadata={'preference_key': key}
        )

    def get_learning_summary(self, subject=None):
        query = f'learning progress {subject}' if subject else 'all learning progress'
        memories = self.recall_memories(query, limit=50)
        return {'total_memories': len(memories), 'memories': memories, 'subject': subject}

    def create_handoff(self, work_description, next_steps):
        handoff = {
            'artifact_id': self._next_id(),
            'work_description': work_description,
            'next_steps': next_steps,
            'created_at': datetime.now().isoformat(),
        }
        self.handoffs.append(handoff)
        return handoff

    def get_prepared_context(self, query, budget=4000):
        memories = self.recall_memories(query, limit=10)
        return {'memories': memories, 'budget': budget}

    def health_check(self):
        return True
