/**
 * seekdb Client for Node.js
 * Handles study material storage and hybrid retrieval.
 * Falls back to in-memory store when seekdb is unavailable.
 */

const crypto = require('crypto');

const SEEKDB_HOST = process.env.SEEKDB_HOST || 'localhost';
const SEEKDB_PORT = parseInt(process.env.SEEKDB_PORT || '2881');
const SEEKDB_USER = process.env.SEEKDB_USER || 'root';
const SEEKDB_PASSWORD = process.env.SEEKDB_PASSWORD || '';
const SEEKDB_DATABASE = process.env.SEEKDB_DATABASE || 'study_mind';

class SeekDBClient {
  constructor(opts = {}) {
    this.host = opts.host || SEEKDB_HOST;
    this.port = opts.port || SEEKDB_PORT;
    this.user = opts.user || SEEKDB_USER;
    this.password = opts.password || SEEKDB_PASSWORD;
    this.database = opts.database || SEEKDB_DATABASE;
    this.available = false;
    this._init();
  }

  _init() {
    try {
      // In a production setup, you would use pyseekdb via Python subprocess
      // or a native seekdb client. For this demo, we use the fallback store
      // and log that seekdb integration is configured.
      console.log(`seekdb configured: ${this.host}:${this.port}/${this.database}`);
      this.available = false; // Will use fallback unless real seekdb connection is established
    } catch (e) {
      console.warn(`seekdb init failed: ${e.message}`);
      this.available = false;
    }
  }

  async healthCheck() {
    return this.available;
  }

  async addMaterial({ title, content, subject, topic, type = 'note', tags = [] }) {
    const docId = crypto.createHash('md5').update(`${title}_${content.substring(0, 50)}`).digest('hex').substring(0, 16);
    return {
      id: docId,
      status: this.available ? 'stored_in_seekdb' : 'fallback_memory',
      metadata: { title, subject, topic, type, tags, created_at: new Date().toISOString() },
      content,
    };
  }

  async search(query, { subject = null, limit = 5, searchType = 'hybrid' } = {}) {
    // In production, this would call seekdb's hybrid search API
    // For now, returns empty results (fallback store handles this)
    return [];
  }

  async getAllMaterials(subject) {
    return [];
  }
}

/**
 * Fallback in-memory material store when seekdb is unavailable.
 */
class FallbackMaterialStore {
  constructor() {
    this.materials = [];
    this._idCounter = 0;
  }

  async healthCheck() {
    return true;
  }

  async addMaterial({ title, content, subject, topic, type = 'note', tags = [] }) {
    const docId = `mat_${++this._idCounter}`;
    const material = {
      id: docId,
      title,
      content,
      subject,
      topic,
      type,
      tags,
      created_at: new Date().toISOString(),
    };
    this.materials.push(material);
    return { id: docId, status: 'stored_in_memory', metadata: material };
  }

  async search(query, { subject = null, limit = 5, searchType = 'hybrid' } = {}) {
    let results = this.materials;
    if (subject) {
      const filtered = results.filter(m => m.subject === subject);
      if (filtered.length > 0) results = filtered;
    }

    const queryLower = query.toLowerCase();
    const queryTokens = queryLower
      .split(/[\s,，、。.;；!！?？]+/)
      .filter(Boolean)
      .flatMap(token => {
        const cjkChars = token.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g);
        if (cjkChars && cjkChars.length >= 2) {
          const words = [];
          for (let i = 0; i < token.length; i++) {
            for (let len = 2; len <= Math.min(4, token.length - i); len++) {
              const sub = token.substring(i, i + len);
              if (/[\u4e00-\u9fff]/.test(sub)) words.push(sub);
            }
          }
          return words.length > 0 ? words : [token];
        }
        return [token];
      });

    const scored = results.map(m => {
      const text = `${m.title} ${m.content} ${m.subject} ${m.topic} ${(m.tags || []).join(' ')}`.toLowerCase();
      const score = queryTokens.reduce((s, w) => s + (text.includes(w) ? 1 : 0), 0);
      return { score, item: m };
    });
    scored.sort((a, b) => b.score - a.score);

    return scored
      .filter(s => s.score > 0)
      .slice(0, limit)
      .map(s => ({
        content: s.item.content,
        metadata: {
          title: s.item.title,
          subject: s.item.subject,
          topic: s.item.topic,
          type: s.item.type,
          tags: s.item.tags,
        },
        score: 1 - (s.score / (queryTokens.length + 1)),
      }));
  }

  async getAllMaterials(subject) {
    if (subject) {
      return this.materials.filter(m => m.subject === subject);
    }
    return [...this.materials];
  }
}

module.exports = { SeekDBClient, FallbackMaterialStore };
