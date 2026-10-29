/**
 * 检索质量评测：在真实浏览器里用不同精度的模型计算向量，
 * 写入 seekdb，再用应用自己的 /search 接口（库内 RRF 混合检索）取回结果，
 * 与人工标注比对 Recall@5 / nDCG@5 / MRR@5。
 *
 * 依赖：
 *   npm i -D playwright && npx playwright install chromium
 * 运行（先启动 seekdb 与应用）：
 *   NODE_PATH=<project>/node_modules node tools/eval.mjs
 *   BASE=http://127.0.0.1:5000 DTYPES=int8,fp32 node tools/eval.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BASE = process.env.BASE || "http://127.0.0.1:5000";
const DTYPES = (process.env.DTYPES || "int8,fp32").split(",").map((s) => s.trim()).filter(Boolean);
const TOPK = 5;
const CORPUS = JSON.parse(fs.readFileSync(path.join(ROOT, "data", "corpus.json"), "utf8")).docs;
const EVAL = JSON.parse(fs.readFileSync(path.join(ROOT, "data", "eval.json"), "utf8"));
const MODES = (process.env.MODES || "hybrid,vector").split(",");
const STAMP = Date.now().toString(36);

const api = async (p, opt = {}) => {
  const res = await fetch(BASE + p, {
    method: opt.method || "GET",
    headers: { "content-type": "application/json", "x-session-id": "eval" },
    body: opt.body ? JSON.stringify(opt.body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(`${p}: ${data.error || res.status}`);
  return data;
};

const recallAt = (ids, rel, k) => {
  const top = ids.slice(0, k);
  return rel.filter((r) => top.includes(r)).length / rel.length;
};
const ndcgAt = (ids, rel, k) => {
  const dcg = ids.slice(0, k).reduce((s, id, i) => (rel.includes(id) ? s + 1 / Math.log2(i + 2) : s), 0);
  const ideal = rel.slice(0, k).reduce((s, _, i) => s + 1 / Math.log2(i + 2), 0);
  return ideal ? dcg / ideal : 0;
};
const mrrAt = (ids, rel, k) => {
  const idx = ids.slice(0, k).findIndex((id) => rel.includes(id));
  return idx >= 0 ? 1 / (idx + 1) : 0;
};

async function main() {
  const browser = await chromium.launch({
    headless: process.env.HEADED !== "1",
    args: ["--no-proxy-server", "--enable-unsafe-webgpu"],
  });
  const page = await browser.newPage();
  page.on("console", (m) => {
    if (m.type() === "error") console.log("  [page:error]", m.text().slice(0, 200));
  });
  console.log(`打开 ${BASE} …`);
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => window.KB?.ready === true, null, { timeout: 120000 });

  const report = { generatedAt: new Date().toISOString(), base: BASE, topK: TOPK, corpusSize: CORPUS.length, queries: EVAL.queries.length, runs: [] };

  for (const dtype of DTYPES) {
    console.log(`\n=== 精度 ${dtype} ===`);
    const info = await page.evaluate(async (dt) => {
      const r = await window.KB.setDtype(dt);
      return { device: r.device, dtype: r.dtype, loadMs: r.loadMs, downloadedMB: r.downloadedMB };
    }, dtype);
    console.log(`  模型：${info.dtype} / ${info.device} / 加载 ${info.loadMs}ms / ${info.downloadedMB ?? "?"}MB`);

    const embedDocs = await page.evaluate(async (texts) => {
      const t0 = performance.now();
      const r = await window.KB.state.embedder.embed(texts, { batchSize: 16 });
      return { vectors: r.vectors, totalMs: performance.now() - t0, docsPerSec: r.docsPerSec, perDocMs: r.perDocMs };
    }, CORPUS.map((d) => `${d.title}。${d.text}`));
    const bad = embedDocs.vectors.filter((v) => v.length !== 512 || v.some((x) => !Number.isFinite(x)));
    const norms = embedDocs.vectors.map((v) => Math.sqrt(v.reduce((s, x) => s + x * x, 0)));
    console.log(
      `  语料向量化：${CORPUS.length} 条 / ${Math.round(embedDocs.totalMs)}ms / ${embedDocs.docsPerSec} docs/s` +
        ` · 非法向量 ${bad.length} · 范数 ${Math.min(...norms).toFixed(3)}~${Math.max(...norms).toFixed(3)}`
    );

    const run = {
      dtype,
      device: info.device,
      loadMs: info.loadMs,
      downloadedMB: info.downloadedMB,
      embedDocsMs: Math.round(embedDocs.totalMs),
      docsPerSec: embedDocs.docsPerSec,
      perDocMs: embedDocs.perDocMs,
      metrics: {},
    };

    for (const mode of MODES) {
      const colName = `eval_${dtype}_${mode}_${STAMP}`;
      await api(`/api/collections/${colName}`, { method: "DELETE" }).catch(() => {});
      await api("/api/collections", { method: "POST", body: { name: colName, dim: 512 } });
      await api(`/api/collections/${colName}/documents`, {
        method: "POST",
        body: {
          batchSize: 64,
          documents: CORPUS.map((d, i) => ({
            id: d.id,
            title: d.title,
            text: `${d.title}。${d.text}`,
            vector: embedDocs.vectors[i],
            metadata: { source: "corpus", title: d.title },
          })),
        },
      });

      // 插入后先用向量探针确认索引已跟上写入（否则首批查询会打在未完全可见的索引上）
      const refresh = await api(`/api/collections/${colName}/refresh`, {
        method: "POST",
        body: {
          probe: { text: `${CORPUS[0].title}。${CORPUS[0].text}`, vector: embedDocs.vectors[0] },
          expectId: CORPUS[0].id,
          mode: "vector",
          retries: 10,
          interval: 500,
          topK: 3,
        },
      });
      console.log(`  [${mode}] 索引自检 visible=${refresh.visible} 尝试 ${refresh.attempts.length} 次 / ${refresh.totalMs}ms`);

      const rows = [];
      for (const item of EVAL.queries) {
        const qv = await page.evaluate(async (q) => {
          const r = await window.KB.state.embedder.embed([q], { batchSize: 1, isQuery: true });
          return r.vectors[0];
        }, item.q);
        const res = await api(`/api/collections/${colName}/search`, {
          method: "POST",
          body: { queryText: item.q, queryVector: qv, topK: TOPK, mode },
        });
        const ids = res.results.map((r) => r.id);
        rows.push({
          query: item.q,
          ids,
          recall: recallAt(ids, item.relevant, TOPK),
          ndcg: ndcgAt(ids, item.relevant, TOPK),
          mrr: mrrAt(ids, item.relevant, TOPK),
        });
      }
      const avg = (k) => +(rows.reduce((s, r) => s + r[k], 0) / rows.length).toFixed(4);
      run["details_" + mode] = rows;
      run.metrics[mode] = {
        recall5: avg("recall"),
        ndcg5: avg("ndcg"),
        mrr5: avg("mrr"),
        missed: rows.filter((r) => r.recall === 0).map((r) => r.query),
      };
      console.log(
        `  [${mode}] Recall@5=${run.metrics[mode].recall5}  nDCG@5=${run.metrics[mode].ndcg5}  MRR@5=${run.metrics[mode].mrr5}  未命中 ${run.metrics[mode].missed.length}/${rows.length}`
      );
      await api(`/api/collections/${colName}`, { method: "DELETE" }).catch(() => {});
    }
    report.runs.push(run);
  }

  await browser.close();

  const baseline = report.runs.find((r) => r.dtype === "fp32");
  if (baseline) {
    for (const r of report.runs) {
      if (r.dtype === "fp32") continue;
      r.delta = {};
      for (const mode of Object.keys(r.metrics)) {
        const b = baseline.metrics[mode];
        const m = r.metrics[mode];
        r.delta[mode] = {
          recall5: +(m.recall5 - b.recall5).toFixed(4),
          ndcg5: +(m.ndcg5 - b.ndcg5).toFixed(4),
          mrr5: +(m.mrr5 - b.mrr5).toFixed(4),
        };
      }
    }
  }

  const out = path.join(ROOT, "docs", process.env.OUT || "eval-report.json");
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(report, null, 2));
  console.log(`\n写入 ${out}`);
}

main().catch((e) => {
  console.error("评测失败:", e.message);
  process.exit(1);
});
