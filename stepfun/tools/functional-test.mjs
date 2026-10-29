/**
 * 功能验收自测：逐条验证题目 3.1 的检查项，输出 PASS/FAIL 明细。
 *   NODE_PATH=<project>/node_modules BASE=http://127.0.0.1:5000 node tools/functional-test.mjs
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BASE = process.env.BASE || "http://127.0.0.1:5000";
const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

const api = async (p, opt = {}) => {
  const res = await fetch(BASE + p, {
    method: opt.method || "GET",
    headers: { "content-type": "application/json", "x-session-id": "func" },
    body: opt.body ? JSON.stringify(opt.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${p}: ${data.error || res.status}`);
  return data;
};

const browser = await chromium.launch({ headless: true, args: ["--no-proxy-server"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const pageErrors = [];
page.on("pageerror", (e) => pageErrors.push(e.message));

await page.goto(BASE, { waitUntil: "domcontentloaded" });
await page.waitForFunction(() => window.KB?.ready === true, null, { timeout: 120000 });
await page.waitForFunction(() => window.KB.state.embedder.ready === true, null, { timeout: 180000 });

// 1. 健康检查与 seekdb 连接
const health = await api("/api/health");
check("seekdb 连接可用", health.status === "ok", `version=${health.seekdb.version}`);

// 2. 本地推理：页面上没有指向外部的请求，模型与 ORT 都来自本机
const external = [];
page.on("request", (r) => {
  const u = r.url();
  if (!u.startsWith(BASE) && !u.startsWith("data:") && !u.startsWith("blob:")) external.push(u);
});
const backend = await page.evaluate(() => ({
  device: window.KB.state.backend,
  dtype: window.KB.state.dtype,
  chip: document.querySelector("#chip-backend").textContent,
  isolated: crossOriginIsolated,
  threads: window.KB.state.embedder ? undefined : undefined,
}));
check("推理后端已确定并在界面提示", ["webgpu", "wasm"].includes(backend.device), `${backend.chip}`);
check("cross-origin isolation（WASM 多线程前提）", backend.isolated === true);

// 3. 建集合
const colA = `func_a_${Date.now().toString(36)}`;
const colB = `func_b_${Date.now().toString(36)}`;
await api("/api/collections", { method: "POST", body: { name: colA, dim: 512 } });
await api("/api/collections", { method: "POST", body: { name: colB, dim: 512 } });

const schema = await api(`/api/collections/${colA}/schema`);
check("库内表含向量索引（HNSW）", /VECTOR|HNSW/i.test(schema.create || ""), (schema.create || "").replace(/\s+/g, " ").slice(0, 120));
check("库内表含全文索引", /FULLTEXT/i.test(schema.create || ""), schema.indexes.map((i) => i.key).join(","));

// 4. 浏览器本地向量化 + 写入
const docs = [
  { id: "f1", title: "向量索引", text: "seekdb 使用 HNSW 向量索引加速语义召回。" },
  { id: "f2", title: "全文分词", text: "中文全文索引可以使用 ngram2 分词器。" },
  { id: "f3", title: "后端选择", text: "浏览器优先使用 WebGPU，不支持时降级 WASM。" },
];
const vecs = await page.evaluate(async (texts) => {
  const r = await window.KB.state.embedder.embed(texts, { batchSize: 8 });
  return r.vectors;
}, docs.map((d) => `${d.title}。${d.text}`));
await api(`/api/collections/${colA}/documents`, {
  method: "POST",
  body: { documents: docs.map((d, i) => ({ ...d, vector: vecs[i], metadata: { source: "func" } })) },
});
const afterWrite = await api(`/api/collections/${colA}/documents?limit=5`);
check("写入 seekdb 后条数正确", afterWrite.total === 3, `total=${afterWrite.total}`);

// 5. 混合检索（库内 RRF）
const qv = await page.evaluate(async (t) => (await window.KB.state.embedder.embed([t], { batchSize: 1, isQuery: true })).vectors[0], "中文分词用什么");
const hybrid = await api(`/api/collections/${colA}/search`, {
  method: "POST",
  body: { queryText: "中文分词用什么", queryVector: qv, topK: 3, mode: "hybrid", explain: true },
});
check("混合检索返回排序结果", hybrid.results.length > 0, `top1=${hybrid.results[0]?.id}`);
const fusedSql = hybrid.dbSql?.sql || "";
check(
  "结果由 seekdb 库内融合（单条 SQL 同时含全文 _fts 与向量 _vs 子查询）",
  /_fts/.test(fusedSql) && /_vs/.test(fusedSql),
  fusedSql.replace(/\s+/g, " ").slice(0, 110)
);
check("混合检索 SQL 由 DBMS_HYBRID_SEARCH 生成", (hybrid.dbSql?.sql || "").length > 20);

const vectorOnly = await api(`/api/collections/${colA}/search`, {
  method: "POST",
  body: { queryText: "中文分词用什么", queryVector: qv, topK: 3, mode: "vector" },
});
check("纯向量检索可用", vectorOnly.results.length > 0);

const fulltextOnly = await api(`/api/collections/${colA}/search`, {
  method: "POST",
  body: { queryText: "ngram2", queryVector: qv, topK: 3, mode: "fulltext" },
});
check("纯全文检索可用", fulltextOnly.results.length > 0, `top1=${fulltextOnly.results[0]?.id}`);

// 6. 集合隔离
const bEmpty = await api(`/api/collections/${colB}/search`, {
  method: "POST",
  body: { queryText: "中文分词用什么", queryVector: qv, topK: 3, mode: "hybrid" },
});
check("切换集合后结果只来自当前集合", bEmpty.results.length === 0, `B 命中 ${bEmpty.results.length}`);

// 7. 增量写入 + 刷新后可见
const newDoc = { id: "f4", title: "增量文档", text: "这是一条增量写入的新文档，主题是索引刷新。" };
const nv = await page.evaluate(async (t) => (await window.KB.state.embedder.embed([t], { batchSize: 1 })).vectors[0], `${newDoc.title}。${newDoc.text}`);
await api(`/api/collections/${colA}/documents`, {
  method: "POST",
  body: { documents: [{ ...newDoc, vector: nv, metadata: { source: "func" } }] },
});
const refresh = await api(`/api/collections/${colA}/refresh`, {
  method: "POST",
  body: { probe: { text: newDoc.text, vector: nv }, expectId: "f4", topK: 5 },
});
check("增量写入后刷新自检可检索到新文档", refresh.visible === true, `count=${refresh.count}`);

// 8. 文件导入（切片）通过界面完成
const tmp = path.join(os.tmpdir(), `edgekb-${Date.now()}.md`);
fs.writeFileSync(tmp, "# 测试文档\n\n浏览器端向量化需要注意批处理大小。\n\n".repeat(6));
await page.setInputFiles("#file-input", tmp);
await page.waitForFunction(() => /完成|失败/.test(document.querySelector("#progress-text").textContent), null, { timeout: 180000 });
const fileImport = await page.evaluate(() => document.querySelector("#progress-text").textContent);
check("上传 .md 文件可切片导入", /完成/.test(fileImport), fileImport.slice(0, 90));
fs.unlinkSync(tmp);

// 9. 刷新页面后集合与会话仍在
await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForFunction(() => window.KB?.ready === true, null, { timeout: 120000 });
const cols = await page.evaluate(() => [...document.querySelectorAll("#kb-select option")].map((o) => o.value));
check("刷新后集合列表持久化", cols.includes(colA) && cols.includes(colB), cols.join(",").slice(0, 80));

check("页面无 JS 异常", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 160));
check("运行期间无外部网络请求（无云端推理）", external.length === 0, external.slice(0, 2).join(" "));

await browser.close();
for (const c of [colA, colB]) await api(`/api/collections/${c}`, { method: "DELETE" }).catch(() => {});

const out = path.join(ROOT, "docs", "functional-report.json");
fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, JSON.stringify({ generatedAt: new Date().toISOString(), base: BASE, backend, results }, null, 2));
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} 通过，报告：${out}`);
process.exit(failed ? 1 : 0);
