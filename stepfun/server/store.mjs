import { SeekdbClient, Schema, VectorIndexConfig, FulltextIndexConfig } from "seekdb";

/** 向量维度：bge-small-zh-v1.5 输出 512 维。 */
export const DIM = Number(process.env.EMBED_DIM || 512);

/** 中文全文索引分词器：ngram2（2~3 字连续切分，无需词典）。 */
const FULLTEXT_ANALYZER = "ngram2";

export function seekdbConfig() {
  return {
    host: process.env.SEEKDB_HOST || "127.0.0.1",
    port: Number(process.env.SEEKDB_PORT || 2881),
    user: process.env.SEEKDB_USER || "root",
    password: process.env.SEEKDB_PASSWORD || "",
    database: process.env.SEEKDB_DATABASE || "test",
    connectTimeout: Number(process.env.SEEKDB_CONNECT_TIMEOUT || 5000),
    // 大批量写入 + HNSW 索引维护在慢盘上可能超过默认 10s，放宽语句超时
    queryTimeout: Number(process.env.SEEKDB_QUERY_TIMEOUT || 60000),
  };
}

export class Store {
  constructor(cfg = seekdbConfig()) {
    this.cfg = cfg;
    this.client = null;
    this.connected = false;
    this.lastError = null;
    this.serverVersion = null;
    this._collections = new Map();
  }

  /** 惰性连接，失败时记录原因（界面会如实展示降级/不可用状态）。 */
  async connect() {
    if (this.connected) return this;
    try {
      await this.client?.close();
    } catch {
      /* 旧连接可能已断开，忽略 */
    }
    this.client = null;
    const client = new SeekdbClient(this.cfg);
    const rows = await client.execute("select version() as v");
    this.serverVersion = rows?.[0]?.v || "unknown";
    // 批量向量写入 + 索引维护需要更长的语句/事务超时（默认 10s 在大批量导入时不够）
    await client.execute("SET SESSION ob_query_timeout = 60000000");
    await client.execute("SET SESSION ob_trx_timeout = 600000000");
    this.client = client;
    this.connected = true;
    this.lastError = null;
    return this;
  }

  async ensure() {
    if (!this.connected) await this.connect();
    return this.client;
  }

  async health() {
    try {
      await this.ensure();
      const rows = await this.client.execute("select 1 as ok");
      return { ok: rows?.[0]?.ok === 1, version: this.serverVersion, error: null };
    } catch (e) {
      this.connected = false;
      this.client = null;
      this.lastError = e.message;
      return { ok: false, version: null, error: e.message };
    }
  }

  /** 建集合：HNSW(cosine) + ngram2 全文索引，双路索引都在 seekdb 内。 */
  async createCollection(name, dim = DIM) {
    await this.ensure();
    const exists = await this.client.hasCollection(name);
    if (exists) return { name, created: false, dimension: dim };
    const collection = await this.client.createCollection({
      name,
      schema: new Schema({
        vectorIndex: new VectorIndexConfig({
          hnsw: { dimension: dim, distance: "cosine" },
          embeddingFunction: null,
        }),
        fulltextIndex: new FulltextIndexConfig(FULLTEXT_ANALYZER, {
          min_ngram_size: 2,
          max_ngram_size: 3,
        }),
      }),
      configuration: null,
      embeddingFunction: null,
    });
    this._collections.set(name, collection);
    return { name, created: true, dimension: collection.dimension };
  }

  async getCollection(name) {
    await this.ensure();
    if (this._collections.has(name)) return this._collections.get(name);
    const exists = await this.client.hasCollection(name);
    if (!exists) {
      const err = new Error(`collection '${name}' 不存在`);
      err.status = 404;
      throw err;
    }
    const collection = await this.client.getCollection({ name, embeddingFunction: null });
    this._collections.set(name, collection);
    return collection;
  }

  async listCollections() {
    await this.ensure();
    const names = await this.client.listCollections({ withEmbeddingFunction: false });
    const out = [];
    for (const c of names) {
      const name = typeof c === "string" ? c : c?.name;
      let count = null;
      try {
        count = await (await this.getCollection(name)).count();
      } catch {
        count = null;
      }
      out.push({ name, count });
    }
    return out;
  }

  async deleteCollection(name) {
    await this.ensure();
    this._collections.delete(name);
    await this.client.deleteCollection(name);
    return { name, deleted: true };
  }

  /**
   * 写入文档（增量）。batchSize 控制每次 add 的条数，返回分段耗时用于性能面板。
   * 向量必须由调用方（浏览器）算好后传入，服务端不做任何向量计算。
   */
  async addDocuments(name, docs, batchSize = 64) {
    const collection = await this.getCollection(name);
    const started = Date.now();
    const batchTimings = [];
    let written = 0;
    for (let i = 0; i < docs.length; i += batchSize) {
      const chunk = docs.slice(i, i + batchSize);
      const t = Date.now();
      await collection.add({
        ids: chunk.map((d) => d.id),
        documents: chunk.map((d) => d.text),
        embeddings: chunk.map((d) => d.vector),
        metadatas: chunk.map((d) => ({ ...(d.metadata || {}), title: d.title || d.metadata?.title || "" })),
      });
      written += chunk.length;
      batchTimings.push({ size: chunk.length, ms: Date.now() - t });
    }
    return { written, totalMs: Date.now() - started, batches: batchTimings };
  }

  /**
   * 混合检索：一次调用 seekdb 的 DBMS_HYBRID_SEARCH（query_string + KNN + RRF），
   * 融合在数据库内完成，服务端不拿两路结果自己拼。
   */
  async search(name, { queryText, queryVector, topK = 5, mode = "hybrid", rankConstant = 60, rankWindowSize = 50 }) {
    const collection = await this.getCollection(name);
    const started = Date.now();
    const include = ["documents", "metadatas", "distances"];

    if (mode === "vector") {
      const res = await collection.query({
        queryEmbeddings: [queryVector],
        nResults: topK,
        include,
      });
      return this._normalize(res, { mode, ms: Date.now() - started, topK });
    }

    if (mode === "fulltext") {
      const res = await collection.hybridSearch({
        query: { whereDocument: { $contains: queryText }, nResults: topK },
        rank: { rrf: { rankWindowSize, rankConstant } },
        nResults: topK,
        include,
      });
      return this._normalize(res, { mode, ms: Date.now() - started, topK });
    }

    const res = await collection.hybridSearch({
      query: { whereDocument: { $contains: queryText }, nResults: topK },
      knn: { queryEmbeddings: [queryVector], nResults: topK },
      rank: { rrf: { rankWindowSize, rankConstant } },
      nResults: topK,
      include,
    });
    return this._normalize(res, { mode, ms: Date.now() - started, topK });
  }

  _normalize(res, meta) {
    const ids = res.ids?.[0] || [];
    const docs = res.documents?.[0] || [];
    const metas = res.metadatas?.[0] || [];
    const dists = res.distances?.[0] || [];
    const results = ids.map((id, i) => ({
      id,
      text: docs[i] || "",
      metadata: metas[i] || {},
      score: Number(dists[i] ?? 0),
      rank: i + 1,
    }));
    return { ...meta, results };
  }

  /** 让 seekdb 生成混合检索实际执行的 SQL，用于技术说明中的证据截图。 */
  async explainHybrid(name, { queryText, queryVector, topK = 5 }) {
    const collection = await this.getCollection(name);
    const table = `c$v2$${collection.collectionId}`;
    const parm = {
      query: { query_string: { fields: ["document"], query: queryText } },
      knn: { field: "embedding", k: topK, query_vector: queryVector },
      rank: { rrf: {} },
      size: topK,
    };
    const literal = JSON.stringify(parm).replace(/\\/g, "\\\\").replace(/'/g, "''");
    await this.client.execute(`SET @search_parm = '${literal}'`);
    const got = await this.client.execute(
      `SELECT DBMS_HYBRID_SEARCH.GET_SQL('${table}', @search_parm) as query_sql FROM dual`
    );
    const sql = String(got?.[0]?.query_sql || "").replace(/^['"]|['"]$/g, "");
    return { table, sql };
  }

  /** seekdb 侧的表结构与索引，用于证明向量/全文索引都建在库里。 */
  async schemaInfo(name) {
    const collection = await this.getCollection(name);
    const table = `c$v2$${collection.collectionId}`;
    const create = await this.client.execute(`SHOW CREATE TABLE \`${table}\``);
    const indexes = await this.client.execute(`SHOW INDEX FROM \`${table}\``);
    return {
      table,
      create: create?.[0] ? Object.values(create[0]).pop() : null,
      indexes: (indexes || []).map((i) => ({
        key: i.Key_name,
        column: i.Column_name,
        type: i.Index_type,
      })),
    };
  }

  async close() {
    try {
      await this.client?.close();
    } catch {
      /* ignore */
    }
  }
}
