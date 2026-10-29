/**
 * 微基准：在同一页面会话内交替测量不同精度 / 不同 batch 的 embedding 吞吐，
 * 减少模型加载与机器噪声带来的偏差（每个配置重复 N 次取中位数）。
 *
 *   NODE_PATH=<project>/node_modules BASE=http://127.0.0.1:5000 node tools/micro-bench.mjs
 * 输出 docs/micro-bench.json
 */
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BASE = process.env.BASE || "http://127.0.0.1:5000";
const REPS = Number(process.env.REPS || 3);
const TEXTS = Number(process.env.TEXTS || 32);
const DTYPES = (process.env.DTYPES || "int8,fp32").split(",");
const BATCHES = (process.env.BATCHES || "1,8,16,32").split(",").map(Number);
const median = (a) => {
  const s = [...a].sort((x, y) => x - y);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

const corpus = JSON.parse(fs.readFileSync(path.join(ROOT, "data", "corpus.json"), "utf8")).docs;
const texts = corpus.slice(0, TEXTS).map((d) => `${d.title}。${d.text}`);

const browser = await chromium.launch({ headless: true, args: ["--no-proxy-server"] });
const page = await browser.newPage();
page.on("pageerror", (e) => console.log("[pageerror]", e.message.slice(0, 200)));
const url = process.env.THREADS ? `${BASE}?threads=${process.env.THREADS}` : BASE;
await page.goto(url, { waitUntil: "domcontentloaded" });
await page.waitForFunction(() => window.KB?.ready === true, null, { timeout: 120000 });

const rows = [];
const embed = (batch) =>
  page.evaluate(
    async ({ texts, batch }) => {
      const t0 = performance.now();
      const r = await window.KB.state.embedder.embed(texts, { batchSize: batch });
      return { ms: performance.now() - t0, perDocMs: r.perDocMs, docsPerSec: r.docsPerSec };
    },
    { texts, batch }
  );

for (const dtype of DTYPES) {
  const info = await page.evaluate(async (dt) => {
    const r = await window.KB.setDtype(dt);
    // 额外预热两批，避免首轮算子/缓存抖动
    await window.KB.state.embedder.embed(["预热一", "预热二", "预热三"], { batchSize: 4 });
    await window.KB.state.embedder.embed(["再预热一次"], { batchSize: 1 });
    return r;
  }, dtype);
  console.log(`${dtype}: ${info.device} / 加载 ${info.loadMs}ms / ${info.downloadedMB ?? "?"}MB`);

  for (const batch of BATCHES) {
    const samples = [];
    for (let i = 0; i < REPS; i++) samples.push(await embed(batch));
    const med = median(samples.map((s) => s.ms));
    const row = {
      dtype,
      device: info.device,
      batch,
      modelMB: info.downloadedMB,
      loadMs: info.loadMs,
      reps: REPS,
      texts: texts.length,
      medianMs: Math.round(med),
      docsPerSec: +(texts.length / (med / 1000)).toFixed(2),
      perDocMs: +(med / texts.length).toFixed(1),
      samples: samples.map((s) => Math.round(s.ms)),
    };
    rows.push(row);
    console.log(`  batch=${batch}  ${row.medianMs}ms  ${row.docsPerSec} docs/s  (${row.samples.join(", ")})`);
  }
}

await browser.close();
const out = path.join(ROOT, "docs", "micro-bench.json");
fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, JSON.stringify({ generatedAt: new Date().toISOString(), base: BASE, threads: process.env.THREADS || "auto", rows }, null, 2));
console.log(`\n写入 ${out}`);
