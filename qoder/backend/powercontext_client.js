/**
 * PowerContext Client for Node.js
 * Handles memory and context management for the learning assistant.
 */

const fetch = require('node-fetch');

const POWERCONTEXT_API_URL = process.env.POWERCONTEXT_API_URL || 'http://localhost:8848';
const POWERCONTEXT_SCOPE_ID = process.env.POWERCONTEXT_SCOPE_ID || 'default_learning_scope';

class PowerContextClient {
  constructor(baseUrl, scopeId) {
    this.baseUrl = (baseUrl || POWERCONTEXT_API_URL).replace(/\/$/, '');
    this.scopeId = scopeId || POWERCONTEXT_SCOPE_ID;
    this.available = null;
  }

  async _request(method, path, body) {
    const url = `${this.baseUrl}${path}`;
    const opts = {
      method,
      headers: {
        'Content-Type': 'application/json',
        'X-Scope-Id': this.scopeId,
      },
      timeout: 10000,
    };
    if (body) opts.body = JSON.stringify(body);
    try {
      const resp = await fetch(url, opts);
      if (resp.ok) {
        this.available = true;
        return await resp.json();
      }
      const text = await resp.text();
      console.warn(`PowerContext ${method} ${path} -> ${resp.status}: ${text}`);
      return null;
    } catch (e) {
      if (this.available !== false) {
        console.warn(`PowerContext unavailable: ${e.message}`);
      }
      this.available = false;
      return null;
    }
  }

  async healthCheck() {
    const result = await this._request('GET', '/api/v1/system/health');
    return result !== null;
  }

  async saveMemory({ content, memoryType = 'topic', metadata = {} }) {
    return this._request('POST', `/api/v1/scopes/${this.scopeId}/memories`, {
      content,
      artifact_type: 'memory',
      family: memoryType,
      metadata,
      source: {
        type: 'journal',
        entry: `Learning entry at ${new Date().toISOString()}`,
      },
    });
  }

  async recallMemories(query, { memoryType = null, limit = 10 } = {}) {
    const params = new URLSearchParams({ query, limit: String(limit) });
    if (memoryType) params.append('family', memoryType);
    const result = await this._request('GET', `/api/v1/scopes/${this.scopeId}/memories/recall?${params}`);
    return result || [];
  }

  async saveExperience({ situation, action, outcome, lesson }) {
    return this._request('POST', `/api/v1/scopes/${this.scopeId}/experiences`, {
      artifact_type: 'experience',
      content: JSON.stringify({ situation, action, outcome, lesson, timestamp: new Date().toISOString() }),
      metadata: { subject: situation.substring(0, 50), tags: ['learning_experience'] },
    });
  }

  async getPreparedContext(query, budget = 4000) {
    return this._request('POST', `/api/v1/scopes/${this.scopeId}/recall/prepared-context`, { query, budget });
  }

  async saveLearningProgress(subject, topic, progressPct, notes = '') {
    return this.saveMemory({
      content: `Progress: ${subject} > ${topic} = ${progressPct}% complete. ${notes}`,
      memoryType: 'topic',
      metadata: { subject, topic, progress: progressPct, timestamp: new Date().toISOString() },
    });
  }

  async saveWeakPoint(subject, topic, description) {
    return this.saveMemory({
      content: `Weak point: ${subject} > ${topic} - ${description}`,
      memoryType: 'insight',
      metadata: { subject, topic, type: 'weak_point', timestamp: new Date().toISOString() },
    });
  }

  async savePreference(key, value) {
    return this.saveMemory({
      content: `Preference: ${key} = ${value}`,
      memoryType: 'preference',
      metadata: { preference_key: key, timestamp: new Date().toISOString() },
    });
  }

  async getLearningSummary(subject) {
    const query = subject ? `learning progress ${subject}` : 'all learning progress';
    const memories = await this.recallMemories(query, { limit: 50 });
    return {
      total_memories: Array.isArray(memories) ? memories.length : 0,
      memories: Array.isArray(memories) ? memories : [],
      subject,
    };
  }

  async createHandoff({ workDescription, nextSteps }) {
    return this._request('POST', `/api/v1/scopes/${this.scopeId}/handoffs`, {
      artifact_type: 'handoff',
      content: JSON.stringify({
        work_description: workDescription,
        next_steps: nextSteps,
        timestamp: new Date().toISOString(),
      }),
      metadata: { type: 'study_session_handoff' },
    });
  }
}

/**
 * Fallback in-memory store when PowerContext server is unavailable.
 */
class FallbackMemoryStore {
  constructor() {
    this.memories = [];
    this.experiences = [];
    this.handoffs = [];
    this._idCounter = 0;
  }

  _nextId() {
    return `fallback_${++this._idCounter}`;
  }

  async healthCheck() {
    return true;
  }

  async saveMemory({ content, memoryType = 'topic', metadata = {} }) {
    const mem = {
      artifact_id: this._nextId(),
      content,
      family: memoryType,
      metadata,
      created_at: new Date().toISOString(),
    };
    this.memories.push(mem);
    return mem;
  }

  async recallMemories(query, { memoryType = null, limit = 10 } = {}) {
    let results = this.memories;
    if (memoryType) {
      results = results.filter(m => m.family === memoryType);
    }
    const queryWords = query.toLowerCase().split(/\s+/);
    const scored = results.map(m => {
      const content = (m.content || '').toLowerCase();
      const score = queryWords.reduce((s, w) => s + (content.includes(w) ? 1 : 0), 0);
      return { score, item: m };
    });
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, limit).map(s => s.item);
  }

  async saveExperience({ situation, action, outcome, lesson }) {
    const exp = {
      artifact_id: this._nextId(),
      situation, action, outcome, lesson,
      created_at: new Date().toISOString(),
    };
    this.experiences.push(exp);
    return exp;
  }

  async saveLearningProgress(subject, topic, progressPct, notes = '') {
    return this.saveMemory({
      content: `Progress: ${subject} > ${topic} = ${progressPct}% complete. ${notes}`,
      memoryType: 'topic',
      metadata: { subject, topic, progress: progressPct },
    });
  }

  async saveWeakPoint(subject, topic, description) {
    return this.saveMemory({
      content: `Weak point: ${subject} > ${topic} - ${description}`,
      memoryType: 'insight',
      metadata: { subject, topic, type: 'weak_point' },
    });
  }

  async savePreference(key, value) {
    return this.saveMemory({
      content: `Preference: ${key} = ${value}`,
      memoryType: 'preference',
      metadata: { preference_key: key },
    });
  }

  async getLearningSummary(subject) {
    const query = subject ? `learning progress ${subject}` : 'all learning progress';
    const memories = await this.recallMemories(query, { limit: 50 });
    return { total_memories: memories.length, memories, subject };
  }

  async createHandoff({ workDescription, nextSteps }) {
    const handoff = {
      artifact_id: this._nextId(),
      work_description: workDescription,
      next_steps: nextSteps,
      created_at: new Date().toISOString(),
    };
    this.handoffs.push(handoff);
    return handoff;
  }

  async getPreparedContext(query, budget = 4000) {
    const memories = await this.recallMemories(query, { limit: 10 });
    return { memories, budget };
  }
}

module.exports = { PowerContextClient, FallbackMemoryStore };
