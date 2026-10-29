import express from "express";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Store } from "./store.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const WEB = path.join(ROOT, "web");
const PORT = Number(process.env.PORT || 5000);
const HOST = process.env.HOST || "0.0.0.0";

const store = new Store();
const app = express();
app.use(express.json({ limit: "64mb" }));

/** 会话状态：记录当前集合与导入计数，按 x-session-id 隔离。 */
const sessions = new Map();
function sessionOf(req) {
  const sid = req.get("x-session-id") || "default";
  if (!sessions.has(sid)) {
    sessions.set(sid, { sid, activeCollection: null, createdAt: Date.now(), imports: 0, searches: 0 });
  }
  return sessions.get(sid);
}

/** WASM 多线程需要 cross-origin isolation；同时保证模型/ONNX 静态资源同源可缓存。 */
const isolationHeaders = (req, res, next) => {
  res.setHeader("Cross-Origin-Opener-Policy", "same-origin");
  res.setHeader("Cross-Origin-Embedder-Policy", "require-corp");
  res.setHeader("Cross-Origin-Resource-Policy", "cross-origin");
  next();
};
app.use(isolationHeaders);

const staticOpts = {
  setHeaders(res, p) {
    if (/\.(onnx|wasm)$/.test(p)) res.setHeader("Cache-Control", "public, max-age=604800");
  },
};
app.use("/models", express.static(path.join(ROOT, "models"), staticOpts));
app.use("/vendor", express.static(path.join(WEB, "vendor"), staticOpts));
app.use(express.static(WEB, staticOpts));

const wrap = (fn) => (req, res) => {
  Promise.resolve(fn(req, res)).catch((e) => {
    const status = e.status || 500;
    res.status(status).json({ error: e.message, hint: status === 503 ? "请先启动 seekdb（见 README）" : undefined });
  });
};

// ---------- 基础状态 ----------
app.get("/api/health", wrap(async (req, res) => {
  const health = await store.health();
  let collections = [];
  if (health.ok) {
    try { collections = await store.listCollections(); } catch { collections = []; }
  }
  res.json({
    status: health.ok ? "ok" : "seekdb-unavailable",
    seekdb: { ...health, config: { host: store.cfg.host, port: store.cfg.port, database: store.cfg.database } },
    embed: { model: "bge-small-zh-v1.5", dim: 512, device: "browser (WebGPU / WASM)", serverSideInference: false },
    collections,
    session: sessionOf(req),
  });
}));

app.get("/api/models", wrap(async (req, res) => {
  const dir = path.join(ROOT, "models", "bge-small-zh-v1.5", "onnx");
  const files = fs.existsSync(dir) ? fs.readdirSync(dir) : [];
  const models = files
    .filter((f) => f.endsWith(".onnx"))
    .map((f) => ({ file: `onnx/${f}`, bytes: fs.statSync(path.join(dir, f)).size }))
    .sort((a, b) => a.bytes - b.bytes);
  res.json({ base: "/models/bge-small-zh-v1.5/", models });
}));

// ---------- 语料 ----------
app.get("/api/corpus", wrap(async (req, res) => {
  const raw = fs.readFileSync(path.join(ROOT, "data", "corpus.json"), "utf8");
  res.type("application/json").send(raw);
}));

// ---------- 集合 ----------
app.get("/api/collections", wrap(async (req, res) => {
  res.json({ collections: await store.listCollections() });
}));

app.post("/api/collections", wrap(async (req, res) => {
  const name = String(req.body?.name || "").trim();
  if (!/^[a-zA-Z_][a-zA-Z0-9_]{1,62}$/.test(name)) {
    return res.status(400).json({ error: "集合名需以字母或下划线开头，2~63 位，只允许字母数字下划线" });
  }
  const out = await store.createCollection(name, Number(req.body?.dim) || 512);
  const s = sessionOf(req);
  s.activeCollection = name;
  res.json({ ...out, session: s });
}));

app.delete("/api/collections/:name", wrap(async (req, res) => {
  const out = await store.deleteCollection(req.params.name);
  const s = sessionOf(req);
  if (s.activeCollection === req.params.name) s.activeCollection = null;
  res.json(out);
}));

app.get("/api/collections/:name/schema", wrap(async (req, res) => {
  res.json(await store.schemaInfo(req.params.name));
}));

// ---------- 写入 ----------
app.post("/api/collections/:name/documents", wrap(async (req, res) => {
  const docs = req.body?.documents;
  if (!Array.isArray(docs) || docs.length === 0) {
    return res.status(400).json({ error: "documents 不能为空" });
  }
  for (const d of docs) {
    if (!d.id || typeof d.text !== "string" || !Array.isArray(d.vector)) {
      return res.status(400).json({ error: "每条文档需要 id / text / vector" });
    }
  }
  const out = await store.addDocuments(req.params.name, docs, Number(req.body?.batchSize) || 64);
  const s = sessionOf(req);
  s.imports += docs.length;
  s.activeCollection = req.params.name;
  res.json({ ...out, session: s });
}));

app.get("/api/collections/:name/documents", wrap(async (req, res) => {
  const collection = await store.getCollection(req.params.name);
  const limit = Math.min(Number(req.query.limit) || 20, 200);
  const offset = Number(req.query.offset) || 0;
  const got = await collection.get({ limit, offset, include: ["documents", "metadatas"] });
  const ids = got.ids || [];
  res.json({
    total: await collection.count(),
    documents: ids.map((id, i) => ({
      id,
      text: got.documents?.[i] || "",
      metadata: got.metadatas?.[i] || {},
    })),
  });
}));

/**
 * 索引刷新 / 一致性自检：写入后确认新数据可被检索，并预热索引。
 * 默认用「纯向量」探针（比混合检索更严格：混合检索可能被全文一路掩盖），
 * 未命中时按 retry × interval 重试，把「索引追上写入」的等待显式记录下来。
 */
app.post("/api/collections/:name/refresh", wrap(async (req, res) => {
  const collection = await store.getCollection(req.params.name);
  const probe = req.body?.probe;
  const expectId = req.body?.expectId;
  const mode = req.body?.mode || "vector";
  const retries = Math.max(1, Number(req.body?.retries) || 4);
  const interval = Math.max(0, Number(req.body?.interval) || 400);
  const t0 = Date.now();
  const count = await collection.count();
  const attempts = [];
  let visible = null;
  let probeResult = null;

  if (probe?.text && Array.isArray(probe.vector)) {
    for (let i = 0; i < retries; i++) {
      const t = Date.now();
      const r = await store.search(req.params.name, {
        queryText: probe.text,
        queryVector: probe.vector,
        topK: Number(req.body?.topK) || 3,
        mode,
      });
      const hitIds = r.results.map((x) => x.id);
      probeResult = { ms: Date.now() - t, hitIds, inDbMs: r.ms, mode };
      visible = expectId ? hitIds.includes(expectId) : null;
      attempts.push({ attempt: i + 1, ms: probeResult.ms, visible, inDbMs: r.ms });
      if (visible !== false) break;
      if (i < retries - 1) await new Promise((r2) => setTimeout(r2, interval));
    }
  }

  res.json({
    count,
    totalMs: Date.now() - t0,
    probe: probeResult,
    attempts,
    visible,
    note: "seekdb HNSW 索引随写入增量维护；此接口用向量探针确认「写入即可检索」，未命中时按 interval 重试。",
  });
}));

// ---------- 检索 ----------
app.post("/api/collections/:name/search", wrap(async (req, res) => {
  const { queryText, queryVector, topK = 5, mode = "hybrid", explain = false } = req.body || {};
  if (!queryText && !queryVector) return res.status(400).json({ error: "需要 queryText 或 queryVector" });
  if (mode !== "fulltext" && !Array.isArray(queryVector)) {
    return res.status(400).json({ error: "向量检索需要 queryVector（由浏览器本地计算）" });
  }
  const s = sessionOf(req);
  s.searches += 1;
  s.activeCollection = req.params.name;
  const out = await store.search(req.params.name, {
    queryText,
    queryVector,
    topK: Number(topK),
    mode,
    rankConstant: Number(req.body?.rankConstant) || 60,
    rankWindowSize: Number(req.body?.rankWindowSize) || 50,
  });
  if (explain) {
    out.dbSql = await store.explainHybrid(req.params.name, {
      queryText: queryText || "",
      queryVector: queryVector || [],
      topK: Number(topK),
    });
  }
  res.json({ ...out, session: s });
}));

// ---------- 会话 ----------
app.get("/api/session", wrap(async (req, res) => res.json(sessionOf(req))));
app.post("/api/session", wrap(async (req, res) => {
  const s = sessionOf(req);
  if (req.body?.activeCollection !== undefined) s.activeCollection = req.body.activeCollection;
  res.json(s);
}));

app.get("/", (req, res) => res.sendFile(path.join(WEB, "index.html")));

const server = app.listen(PORT, HOST, () => {
  const cfg = store.cfg;
  console.log(`[edge-kb] http://localhost:${PORT}  seekdb=${cfg.host}:${cfg.port}/${cfg.database}`);
  store.health().then((h) => {
    console.log(`[edge-kb] seekdb ${h.ok ? "ok " + h.version : "UNAVAILABLE: " + h.error}`);
  });
});

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    server.close(() => store.close().finally(() => process.exit(0)));
  });
}
