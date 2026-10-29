import { Embedder, detectWebGPU, median, EMBED_DIM, WASM_THREADS } from "/embedder.js";

const $ = (s) => document.querySelector(s);
const el = {
  kbSelect: $("#kb-select"),
  kbName: $("#kb-name"),
  btnCreate: $("#btn-create"),
  btnDrop: $("#btn-drop"),
  btnSchema: $("#btn-schema"),
  fileInput: $("#file-input"),
  dropzone: $("#dropzone"),
  btnSample: $("#btn-sample"),
  btnRefresh: $("#btn-refresh"),
  batchSize: $("#batch-size"),
  batchLabel: $("#batch-label"),
  barFill: $("#bar-fill"),
  progressText: $("#progress-text"),
  kbStats: $("#kb-stats"),
  query: $("#query"),
  btnSearch: $("#btn-search"),
  mode: $("#mode"),
  topk: $("#topk"),
  explain: $("#explain"),
  results: $("#results"),
  sqlBox: $("#sql-box"),
  sqlText: $("#sql-text"),
  deviceMode: $("#device-mode"),
  dtypeMode: $("#dtype-mode"),
  threadsMode: $("#threads-mode"),
  btnLoadModel: $("#btn-load-model"),
  btnBench: $("#btn-bench"),
  perfTable: $("#perf-table"),
  benchOut: $("#bench-out"),
  log: $("#log"),
  chipSeekdb: $("#chip-seekdb"),
  chipBackend: $("#chip-backend"),
  chipModel: $("#chip-model"),
  banner: $("#banner"),
};

const SID = (() => {
  let s = localStorage.getItem("edgekb-sid");
  if (!s) {
    s = globalThis.crypto?.randomUUID?.() || `s-${Date.now()}`;
    localStorage.setItem("edgekb-sid", s);
  }
  return s;
})();

const state = {
  health: null,
  collections: [],
  active: null,
  embedder: new Embedder(),
  backend: null,
  backendReason: null,
  dtype: "int8",
  models: [],
  timings: [],
  bench: null,
  busy: false,
};

// ---------------- 基础工具 ----------------
async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "content-type": "application/json", "x-session-id": SID },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

const fmtMs = (ms) => (ms == null ? "—" : ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`);

function logTiming(phase, ms, note = "") {
  state.timings.push({ phase, ms: Math.round(ms), note, at: new Date().toISOString() });
  const row = el.perfTable.querySelector(`tr[data-k="${phase}"]`);
  if (row) {
    row.children[1].textContent = fmtMs(ms);
    row.children[2].textContent = note;
  }
  el.log.textContent = state.timings
    .slice(-40)
    .map((t) => `${t.at.slice(11, 23)}  ${t.phase.padEnd(14)} ${String(fmtMs(t.ms)).padStart(9)}  ${t.note}`)
    .join("\n");
}

function setProgress(pct, text) {
  el.barFill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  if (text) el.progressText.textContent = text;
}

function banner(msg, kind = "warn") {
  if (!msg) {
    el.banner.classList.add("hidden");
    return;
  }
  el.banner.textContent = msg;
  el.banner.classList.remove("hidden");
  el.banner.style.borderColor = kind === "err" ? "#f5c7c9" : "#f3ddbd";
  el.banner.style.background = kind === "err" ? "#fff5f5" : "#fff8ed";
  el.banner.style.color = kind === "err" ? "#c0262d" : "#8a5a0b";
}

function chip(node, text, kind = "mute", title) {
  node.textContent = text;
  node.className = `chip chip-${kind}`;
  if (title) node.title = title;
}

// ---------------- 状态展示 ----------------
async function refreshHealth() {
  try {
    const h = await api("/api/health");
    state.health = h;
    state.collections = h.collections || [];
    if (h.status === "ok") {
      chip(el.chipSeekdb, `seekdb · 已连接 ${h.seekdb.version}`, "ok", `${h.seekdb.config.host}:${h.seekdb.config.port}/${h.seekdb.config.database}`);
      banner("");
    } else {
      chip(el.chipSeekdb, "seekdb · 未连接", "err", h.seekdb.error || "");
      banner(
        `seekdb 不可用：${h.seekdb.error || "未知错误"}。请先启动 seekdb（README「启动 seekdb」一节），本应用不会用内存或其它数据库替代 seekdb。`,
        "err"
      );
    }
    renderCollectionOptions();
  } catch (e) {
    chip(el.chipSeekdb, "seekdb · 后端未启动", "err", e.message);
    banner(`后端服务不可用：${e.message}`, "err");
  }
}

function renderCollectionOptions() {
  const list = state.collections || [];
  const counts = new Map(list.map((c) => [c.name, c.count]));
  // 保留当前会话选中的集合：即使 health 轮询的数据一时没刷新到新集合，也不能把 active 清空
  const names = list.map((c) => c.name);
  if (state.active && !names.includes(state.active)) names.unshift(state.active);
  el.kbSelect.innerHTML = names.length
    ? names.map((n) => `<option value="${n}">${n}（${counts.get(n) ?? "?"} 条）</option>`).join("")
    : `<option value="">（暂无集合）</option>`;
  if (state.active && names.includes(state.active)) el.kbSelect.value = state.active;
  state.active = el.kbSelect.value || null;
  renderKbStats();
}

function renderKbStats() {
  const cur = (state.collections || []).find((c) => c.name === state.active);
  const e = state.embedder;
  el.kbStats.innerHTML = [
    `集合：<b>${state.active || "—"}</b>`,
    `文档数：<b>${cur?.count ?? "—"}</b>`,
    `后端：<b>${state.backend || "—"}</b> · 精度：<b>${state.dtype.toUpperCase()}</b> · WASM 线程：<b>${WASM_THREADS}</b>`,
    `模型已加载：<b>${e.ready ? "是" : "否"}</b>${e.ready ? `（加载 ${fmtMs(e.loadMs)}，预热 ${fmtMs(e.warmMs)}）` : ""}`,
  ].join("  ·  ");
}

// ---------------- 模型 ----------------
const DTYPE_FILE = { int8: "onnx/model_int8.onnx", fp16: "onnx/model_fp16.onnx", fp32: "onnx/model.onnx" };

async function loadModelsMeta() {
  try {
    const m = await api("/api/models");
    state.models = m.models || [];
  } catch {
    state.models = [];
  }
}

async function ensureModel({ force = false } = {}) {
  const e = state.embedder;
  const dtype = el.dtypeMode.value || "int8";
  if (e.ready && !force && e.dtype === dtype && e.device === state.backend) return e;

  const wanted = DTYPE_FILE[dtype];
  const available = state.models.some((m) => m.file === wanted);
  if (!available) {
    const msg = `当前模型目录缺少 ${wanted}，已回退到 INT8。可执行 tools/fetch_model.sh --with-${dtype} 下载后再试。`;
    banner(msg, "warn");
    el.dtypeMode.value = "int8";
  }
  state.dtype = el.dtypeMode.value;

  const mode = el.deviceMode.value;
  let device = state.backend;
  if (mode === "wasm") {
    device = "wasm";
    state.backendReason = "用户选择强制 WASM（验证降级路径）";
  } else if (mode === "webgpu") {
    const g = await detectWebGPU();
    device = g.ok ? "webgpu" : "wasm";
    state.backendReason = g.ok ? "用户选择强制 WebGPU" : `强制 WebGPU 失败，降级 WASM：${g.reason}`;
  }

  chip(el.chipBackend, `推理后端 · 加载中（${device}）`, "warn");
  setProgress(2, `加载模型 ${state.dtype.toUpperCase()}（${device}）…`);
  try {
    let info;
    try {
      info = await e.load({
        device,
        dtype: state.dtype,
        onProgress: (p) => {
          if (p?.status === "progress" && p.total) {
            const pct = Math.round((p.loaded / p.total) * 100);
            setProgress(pct, `下载 ${p.file}：${pct}%（${(p.loaded / 1048576).toFixed(1)} / ${(p.total / 1048576).toFixed(1)} MB）`);
          }
        },
      });
    } catch (gpuErr) {
      // WebGPU 运行时初始化失败（驱动/算子不支持等）时，自动回退 WASM，保证核心流程可用
      if (device !== "webgpu") throw gpuErr;
      state.backendReason = `WebGPU 初始化失败，已回退 WASM：${gpuErr.message}`;
      info = await e.load({
        device: "wasm",
        dtype: state.dtype,
        onProgress: (p) => {
          if (p?.status === "progress" && p.total) {
            const pct = Math.round((p.loaded / p.total) * 100);
            setProgress(pct, `下载 ${p.file}：${pct}%`);
          }
        },
      });
    }
    state.backend = info.device;
    logTiming("model-load", info.loadMs, `${state.dtype.toUpperCase()} / ${info.device} · 预热 ${info.warmMs}ms`);
    const warn = info.device === "wasm" ? `（已降级：${state.backendReason || "WebGPU 不可用"}）` : "";
    chip(el.chipBackend, `推理后端 · ${info.device.toUpperCase()}${warn}`, info.device === "webgpu" ? "ok" : "warn", state.backendReason || "");
    chip(el.chipModel, `模型 · ${state.dtype.toUpperCase()} · ${info.downloadedMB ?? "?"} MB`, "ok", "bge-small-zh-v1.5 / 512 维 / CLS 池化 + L2 归一化");
    if (info.device === "wasm" && state.backendReason) banner(`WebGPU 不可用，已自动降级到 WASM：${state.backendReason}。核心流程仍可正常使用。`, "warn");
    setProgress(100, `模型就绪：${state.dtype.toUpperCase()} / ${info.device.toUpperCase()}，加载 ${fmtMs(info.loadMs)}`);
    renderKbStats();
    return e;
  } catch (err) {
    chip(el.chipBackend, "推理后端 · 加载失败", "err");
    banner(`模型加载失败：${err.message}`, "err");
    throw err;
  }
}

async function detectAndSetBackend() {
  const g = await detectWebGPU();
  if (g.ok) {
    state.backend = "webgpu";
    state.backendReason = `WebGPU 可用（${g.adapter}）`;
    chip(el.chipBackend, "推理后端 · WebGPU 可用", "ok", state.backendReason);
  } else {
    state.backend = "wasm";
    state.backendReason = g.reason;
    chip(el.chipBackend, "推理后端 · WASM（WebGPU 不可用）", "warn", g.reason);
  }
}

// ---------------- 集合 ----------------
async function createCollection() {
  const name = el.kbName.value.trim();
  if (!name) return banner("请输入集合名，例如 kb_demo", "warn");
  try {
    await ensureModel();
    const out = await api("/api/collections", { method: "POST", body: { name, dim: EMBED_DIM } });
    state.active = out.name;
    el.kbName.value = "";
    await refreshHealth();
    el.kbSelect.value = out.name;
    state.active = out.name;
    banner("");
    setProgress(0, `集合 ${out.name} 就绪（HNSW cosine ${EMBED_DIM} 维 + ngram2 全文索引）`);
  } catch (e) {
    banner(`创建集合失败：${e.message}`, "err");
  }
}

async function dropCollection() {
  const name = state.active || el.kbSelect.value;
  if (!name) return;
  if (!confirm(`确认删除集合 ${name}？该操作不可恢复。`)) return;
  try {
    await api(`/api/collections/${name}`, { method: "DELETE" });
    state.active = null;
    await refreshHealth();
  } catch (e) {
    banner(`删除失败：${e.message}`, "err");
  }
}

async function showSchema() {
  if (!state.active) return banner("请先选择集合", "warn");
  try {
    const s = await api(`/api/collections/${state.active}/schema`);
    el.sqlBox.classList.remove("hidden");
    el.sqlText.textContent = `-- seekdb 表结构：${s.table}\n${s.create}\n\n-- 索引\n${s.indexes.map((i) => `${i.key}  column=${i.column}  type=${i.type}`).join("\n")}`;
  } catch (e) {
    banner(`读取表结构失败：${e.message}`, "err");
  }
}

// ---------------- 导入 ----------------
function chunkText(text, { size = 320, overlap = 60 } = {}) {
  const clean = text.replace(/\r\n/g, "\n").trim();
  if (clean.length <= size) return [clean];
  const chunks = [];
  for (let i = 0; i < clean.length; i += size - overlap) {
    chunks.push(clean.slice(i, i + size));
    if (i + size >= clean.length) break;
  }
  return chunks;
}

function uid() {
  return Math.random().toString(36).slice(2, 10);
}

async function importDocuments(docs, { label = "语料" } = {}) {
  if (state.busy) return banner("已有导入任务在执行", "warn");
  if (!docs.length) return;
  state.busy = true;
  el.btnSample.disabled = el.fileInput.disabled = el.btnSearch.disabled = true;
  try {
    await ensureModel();
    if (!state.active) {
      const name = `kb_${new Date().toISOString().slice(5, 10).replace("-", "")}_${uid()}`;
      await api("/api/collections", { method: "POST", body: { name, dim: EMBED_DIM } });
      state.active = name;
      await refreshHealth();
      el.kbSelect.value = name;
    }
    const batchSize = Number(el.batchSize.value) || 16;
    const tAll = performance.now();
    let embedMs = 0;
    let writeMs = 0;
    let done = 0;
    let lastVector = null;
    setProgress(0, `准备导入 ${docs.length} 条${label}…`);

    for (let i = 0; i < docs.length; i += batchSize) {
      const batch = docs.slice(i, i + batchSize);
      const r = await state.embedder.embed(batch.map((d) => d.text), {
        batchSize,
        onBatch: ({ done: d, total }) => setProgress(((i + d) / docs.length) * 90, `向量化 ${i + d}/${docs.length}`),
      });
      embedMs += r.totalMs;
      const payload = {
        batchSize: batch.length,
        documents: batch.map((d, k) => ({
          id: d.id,
          title: d.title || "",
          text: d.text,
          vector: r.vectors[k],
          metadata: { source: d.source || label, title: d.title || "", chunk: d.chunk ?? 0 },
        })),
      };
      const w = await api(`/api/collections/${state.active}/documents`, { method: "POST", body: payload });
      writeMs += w.totalMs;
      lastVector = r.vectors[batch.length - 1];
      done += batch.length;
      setProgress((done / docs.length) * 90, `写入 seekdb ${done}/${docs.length}（本批 ${fmtMs(w.totalMs)}）`);
    }

    // 写入完成后做一次一致性自检：用最后一条文档的向量做探针查询，确认新数据可检索
    const last = docs[docs.length - 1];
    const tR = performance.now();
    const refresh = await api(`/api/collections/${state.active}/refresh`, {
      method: "POST",
      body: { probe: { text: last.text, vector: lastVector }, expectId: last.id, topK: 3 },
    });
    const refreshMs = performance.now() - tR;

    logTiming("embed-docs", embedMs, `${docs.length} 条 / ${(docs.length / (embedMs / 1000)).toFixed(1)} docs/s · batch ${batchSize}`);
    logTiming("seekdb-write", writeMs, `${docs.length} 条写入（含 HNSW/全文索引增量维护）`);
    logTiming(
      "refresh",
      refreshMs,
      `向量探针 visible=${refresh.visible} / count=${refresh.count} / 尝试 ${refresh.attempts?.length ?? 1} 次`
    );

    setProgress(100, `完成：${done} 条，向量化 ${fmtMs(embedMs)}，写入 ${fmtMs(writeMs)}，自检 ${refresh.visible ? "可见" : "不可见"}`);
    await refreshHealth();
    renderKbStats();
    if (refresh.visible === false) banner("写入后自检未命中新文档，请检查 seekdb 索引状态。", "err");
  } catch (e) {
    banner(`导入失败：${e.message}`, "err");
    setProgress(0, `导入失败：${e.message}`);
  } finally {
    state.busy = false;
    el.btnSample.disabled = el.fileInput.disabled = el.btnSearch.disabled = false;
  }
}

async function importSample() {
  const res = await fetch("/api/corpus");
  const corpus = await res.json();
  const docs = corpus.docs.map((d) => ({ id: d.id, title: d.title, text: `${d.title}。${d.text}`, source: "sample-corpus" }));
  await importDocuments(docs, { label: "示例语料" });
}

async function importFiles(files) {
  const docs = [];
  for (const f of files) {
    const text = await f.text();
    const pieces = chunkText(text);
    pieces.forEach((p, i) => {
      docs.push({ id: `${f.name.replace(/\W+/g, "-")}-${i}-${uid()}`, title: `${f.name}#${i + 1}`, text: p, source: f.name, chunk: i });
    });
  }
  await importDocuments(docs, { label: `${files.length} 个文件` });
}

// ---------------- 检索 ----------------
function scoreLabel(mode) {
  return mode === "vector" ? "cosine 距离" : "RRF 分数";
}

function renderResults(data) {
  const mode = data.mode;
  if (!data.results.length) {
    el.results.innerHTML = `<div class="empty">没有命中。可尝试换用「混合检索」或更换关键词。</div>`;
    return;
  }
  el.results.innerHTML = data.results
    .map((r, i) => {
      const title = r.metadata?.title || r.id;
      return `<article class="card ${i === 0 ? "hit-top" : ""}">
        <div class="card-head">
          <span class="rank">${r.rank}</span>
          <span class="card-title">${escapeHtml(String(title))}</span>
          <span class="score">${scoreLabel(mode)} ${r.score.toFixed(4)}</span>
        </div>
        <div class="card-body">${escapeHtml(r.text).slice(0, 260)}</div>
        <div class="card-meta">
          <span>id: ${escapeHtml(r.id)}</span>
          <span>来源: ${escapeHtml(String(r.metadata?.source ?? "—"))}</span>
          <span>库内检索 ${fmtMs(data.ms)}</span>
        </div>
      </article>`;
    })
    .join("");
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function search({ queryText, record = true } = {}) {
  const q = queryText ?? el.query.value.trim();
  if (!q) return banner("请输入查询内容", "warn");
  if (!state.active) return banner("请先把语料导入某个集合", "warn");
  el.btnSearch.disabled = true;
  const t0 = performance.now();
  try {
    await ensureModel();
    const t1 = performance.now();
    const qv = await state.embedder.embed([q], { batchSize: 1, isQuery: true });
    logTiming("query-embed", performance.now() - t1, `${state.dtype.toUpperCase()} / ${state.backend.toUpperCase()} · 1 条`);
    const t2 = performance.now();
    const data = await api(`/api/collections/${state.active}/search`, {
      method: "POST",
      body: {
        queryText: q,
        queryVector: qv.vectors[0],
        topK: Number(el.topk.value),
        mode: el.mode.value,
        explain: el.explain.checked,
      },
    });
    const t3 = performance.now();
    logTiming("seekdb-search", t3 - t2, `${data.mode} · top${data.topK} · 库内 ${fmtMs(data.ms)}`);
    renderResults(data);
    if (data.dbSql?.sql) {
      el.sqlBox.classList.remove("hidden");
      el.sqlText.textContent = `-- seekdb 生成的混合检索 SQL（表 ${data.dbSql.table}）\n${data.dbSql.sql}`;
    }
    const e2e = performance.now() - t0;
    logTiming("e2e", e2e, `${state.backend.toUpperCase()} / ${state.dtype.toUpperCase()} / ${data.mode}`);
    const result = { e2e, queryEmbedMs: t2 - t1, dbMs: t3 - t2, inDbMs: data.ms, mode: data.mode };
    if (record) state.lastSearch = result;
    return result;
  } catch (e) {
    banner(`检索失败：${e.message}`, "err");
    throw e;
  } finally {
    el.btnSearch.disabled = false;
  }
}

const BENCH_QUERIES = [
  "中文全文索引用什么分词器",
  "RRF 是怎么融合两路召回的",
  "WebGPU 不支持时怎么办",
  "INT8 量化会让检索变差吗",
  "跨域隔离和 SharedArrayBuffer",
  "浏览器里怎么缓存模型",
  "HNSW 参数怎么调",
  "怎么评估召回质量",
  "增量导入后多久能检索到",
  "bge 模型的查询前缀",
];

async function bench(rounds = 10) {
  if (!state.active) return banner("请先导入语料", "warn");
  el.btnBench.disabled = true;
  setProgress(0, "基准测试：预热 + 10 次查询…");
  try {
    await ensureModel();
    await search({ queryText: BENCH_QUERIES[0], record: false }); // 预热
    const rows = [];
    for (let i = 0; i < rounds; i++) {
      const r = await search({ queryText: BENCH_QUERIES[i % BENCH_QUERIES.length], record: false });
      if (!r) throw new Error("查询未执行（集合未选择或模型未就绪）");
      rows.push(r);
      setProgress(((i + 1) / rounds) * 100, `基准测试 ${i + 1}/${rounds}`);
    }
    const e2e = rows.map((r) => r.e2e);
    const db = rows.map((r) => r.dbMs);
    const qe = rows.map((r) => r.queryEmbedMs);
    state.bench = {
      backend: state.backend,
      dtype: state.dtype,
      mode: el.mode.value,
      topK: Number(el.topk.value),
      collection: state.active,
      rounds,
      e2eMedian: median(e2e),
      e2eMin: Math.min(...e2e),
      e2eMax: Math.max(...e2e),
      dbMedian: median(db),
      queryEmbedMedian: median(qe),
      raw: rows,
    };
    el.benchOut.innerHTML = [
      `中位数端到端：<b>${fmtMs(state.bench.e2eMedian)}</b>（min ${fmtMs(state.bench.e2eMin)} / max ${fmtMs(state.bench.e2eMax)}）`,
      `中位数查询向量化：<b>${fmtMs(state.bench.queryEmbedMedian)}</b> · 中位数 seekdb 往返：<b>${fmtMs(state.bench.dbMedian)}</b>`,
      `配置：${state.bench.backend.toUpperCase()} / ${state.bench.dtype.toUpperCase()} / ${state.bench.mode} / top${state.bench.topK}`,
    ].join("<br>");
    window.__bench = state.bench;
    setProgress(100, `基准完成：中位数 ${fmtMs(state.bench.e2eMedian)}`);
  } catch (e) {
    banner(`基准测试失败：${e.message}`, "err");
  } finally {
    el.btnBench.disabled = false;
  }
}

// ---------------- 事件与初始化 ----------------
function wire() {
  el.batchSize.addEventListener("input", () => (el.batchLabel.textContent = el.batchSize.value));
  el.btnCreate.addEventListener("click", createCollection);
  el.btnDrop.addEventListener("click", dropCollection);
  el.btnSchema.addEventListener("click", showSchema);
  el.btnSample.addEventListener("click", importSample);
  el.btnRefresh.addEventListener("click", async () => {
    if (!state.active) return banner("请先选择集合", "warn");
    const t = performance.now();
    try {
      const r = await api(`/api/collections/${state.active}/refresh`, { method: "POST", body: {} });
      logTiming("refresh", performance.now() - t, `count=${r.count}`);
      setProgress(100, `集合 ${state.active} 共 ${r.count} 条，索引一致性校验完成`);
    } catch (e) {
      banner(`刷新失败：${e.message}`, "err");
    }
  });
  el.btnSearch.addEventListener("click", () => search());
  el.query.addEventListener("keydown", (e) => {
    if (e.key === "Enter") search();
  });
  el.btnLoadModel.addEventListener("click", () => ensureModel({ force: true }).catch(() => {}));
  el.btnBench.addEventListener("click", () => bench(10));
  el.deviceMode.addEventListener("change", () => ensureModel({ force: true }).catch(() => {}));
  const savedThreads = localStorage.getItem("edgekb-threads");
  el.threadsMode.value = savedThreads || "auto";
  el.threadsMode.addEventListener("change", () => {
    if (el.threadsMode.value === "auto") localStorage.removeItem("edgekb-threads");
    else localStorage.setItem("edgekb-threads", el.threadsMode.value);
    location.reload();
  });
  el.dtypeMode.addEventListener("change", () => ensureModel({ force: true }).catch(() => {}));
  el.kbSelect.addEventListener("change", () => {
    state.active = el.kbSelect.value;
    renderKbStats();
    api("/api/session", { method: "POST", body: { activeCollection: state.active } }).catch(() => {});
  });

  el.dropzone.addEventListener("click", () => el.fileInput.click());
  el.fileInput.addEventListener("change", () => {
    if (el.fileInput.files?.length) importFiles([...el.fileInput.files]);
  });
  for (const ev of ["dragover", "dragenter"]) {
    el.dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      el.dropzone.classList.add("drag");
    });
  }
  for (const ev of ["dragleave", "drop"]) {
    el.dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      el.dropzone.classList.remove("drag");
    });
  }
  el.dropzone.addEventListener("drop", (e) => {
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) importFiles(files);
  });

  // 离线/在线提示
  window.addEventListener("offline", () => banner("网络已断开：模型与本机 seekdb 仍在本地工作，检索不受影响。", "warn"));
}

async function init() {
  wire();
  await loadModelsMeta();
  await detectAndSetBackend();
  await refreshHealth();
  setInterval(refreshHealth, 15000);
  renderKbStats();
  // 预加载模型，避免用户第一次点检索时才等下载
  ensureModel().catch(() => {});
}

window.KB = {
  state,
  api,
  importSample,
  search,
  bench,
  ensureModel,
  setBackend: async (mode) => {
    el.deviceMode.value = mode;
    return ensureModel({ force: true });
  },
  setDtype: async (dtype) => {
    el.dtypeMode.value = dtype;
    return ensureModel({ force: true });
  },
  setConfig: async ({ device, dtype }) => {
    if (device) el.deviceMode.value = device;
    if (dtype) el.dtypeMode.value = dtype;
    return ensureModel({ force: true });
  },
  ready: false,
};

init()
  .then(() => {
    window.KB.ready = true;
  })
  .catch((e) => banner(`初始化失败：${e.message}`, "err"));
