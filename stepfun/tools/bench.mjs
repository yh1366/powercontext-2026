/**
 * 端到端基准：用真实浏览器驱动应用完成「导入 → 向量化 → 写 seekdb → 混合检索」，
 * 收集分阶段耗时、吞吐与端到端延迟，并截图。
 *
 *   npm i -D playwright && npx playwright install chromium
 *   BASE=http://127.0.0.1:5000 CONFIGS="wasm:int8,webgpu:int8" node tools/bench.mjs
 *
 * CONFIGS 用 device:dtype 表示；每种配置分冷启动（新上下文，无模型缓存）
 * 与热启动（同上下文刷新页面，命中 Cache Storage）两轮。
 */
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BASE = process.env.BASE || "http://127.0.0.1:5000";
const CONFIGS = (process.env.CONFIGS || "wasm:int8").split(",").map((s) => {
  const [device, dtype] = s.split(":").map((x) => x.trim());
  return { device, dtype };
});
const OUT_DIR = path.join(ROOT, "docs");
const IMG_DIR = path.join(OUT_DIR, "images");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(p, opt = {}) {
  const res = await fetch(BASE + p, {
    method: opt.method || "GET",
    headers: { "content-type": "application/json", "x-session-id": "bench" },
    body: opt.body ? JSON.stringify(opt.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${p}: ${data.error || res.status}`);
  return data;
}

async function waitReady(page) {
  await page.waitForFunction(() => window.KB?.ready === true, null, { timeout: 180000 });
}

const CLEANUP = [];
async function runOnce(page, cfg, phase) {
  const stamp = Date.now().toString(36);
  const colName = `bench_${cfg.device}_${cfg.dtype}_${stamp}`;
  console.log(`  [${cfg.device}/${cfg.dtype}/${phase}] 集合 ${colName}`);

  await page.evaluate(
    async ({ name, batch, device, dtype }) => {
      window.KB.state.timings.length = 0;
      window.__bench = null;
      const slider = document.querySelector("#batch-size");
      slider.value = String(batch);
      slider.dispatchEvent(new Event("input"));
      const r = await window.KB.setConfig({ device, dtype });
      window.__modelInfo = r;
      await window.KB.api("/api/collections", { method: "POST", body: { name, dim: 512 } });
      window.KB.state.active = name;
    },
    { name: colName, batch: Number(process.env.BATCH || 16), device: cfg.device, dtype: cfg.dtype }
  );

  const t0 = Date.now();
  await page.evaluate(() => window.KB.importSample());
  await page.waitForFunction(() => window.KB.state.busy === false && !document.querySelector("#btn-sample").disabled, null, {
    timeout: 300000,
  });
  const importWallMs = Date.now() - t0;
  const progress = await page.evaluate(() => document.querySelector("#progress-text")?.textContent);
  const bannerText = await page.evaluate(() => document.querySelector("#banner")?.textContent);
  if (/失败|没有命中/.test(progress || "") || /失败/.test(bannerText || "")) {
    throw new Error(`导入失败：${progress} / ${bannerText}`);
  }

  const stats = await api(`/api/collections/${colName}/documents?limit=1`);
  await page.evaluate((n) => {
    window.KB.state.active = n;
  }, colName);
  const benchErrors = [];
  page.on("pageerror", (e) => benchErrors.push(e.message));
  await page.evaluate(async () => {
    await window.KB.bench(10);
  });
  const bench = await page.evaluate(() => window.__bench);
  if (!bench) {
    const bannerText = await page.evaluate(() => document.querySelector("#banner")?.textContent);
    throw new Error(`基准查询未产出结果：${bannerText} ${benchErrors.join(" | ")}`);
  }
  const timings = await page.evaluate(() => window.KB.state.timings.slice());
  const modelInfo = await page.evaluate(() => window.__modelInfo);
  const shot = path.join(IMG_DIR, `ui-${cfg.device}-${cfg.dtype}-b${process.env.BATCH || 16}-${phase}.png`);
  await page.screenshot({ path: shot, fullPage: false });

  const pick = (phaseName) => timings.filter((t) => t.phase === phaseName).map((t) => t.ms);
  const row = {
    device: cfg.device,
    dtype: cfg.dtype,
    threads: process.env.THREADS || 'auto',
    phase,
    collection: colName,
    batch: Number(process.env.BATCH || 16),
    docs: stats.total,
    modelLoadMs: pick("model-load").at(-1) ?? null,
    modelWarmMs: modelInfo?.warmMs ?? null,
    downloadedMB: modelInfo?.downloadedMB ?? null,
    embedDocsMs: pick("embed-docs").at(-1) ?? null,
    seekdbWriteMs: pick("seekdb-write").at(-1) ?? null,
    refreshMs: pick("refresh").at(-1) ?? null,
    importWallMs,
    docsPerSec: pick("embed-docs").at(-1) ? +((stats.total / pick("embed-docs").at(-1)) * 1000).toFixed(2) : null,
    queryEmbedMedianMs: bench?.queryEmbedMedian ?? null,
    seekdbSearchMedianMs: bench?.dbMedian ?? null,
    e2eMedianMs: bench?.e2eMedian ?? null,
    e2eMinMs: bench?.e2eMin ?? null,
    e2eMaxMs: bench?.e2eMax ?? null,
    mode: bench?.mode,
    topK: bench?.topK,
    screenshot: path.relative(ROOT, shot),
    rawBench: bench,
    timings,
  };
  console.log(
    `    → 模型 ${row.modelLoadMs}ms / 向量化 ${row.embedDocsMs}ms (${row.docsPerSec} docs/s) / 写入 ${row.seekdbWriteMs}ms / e2e 中位数 ${row.e2eMedianMs?.toFixed?.(1)}ms`
  );

  CLEANUP.push(colName);
  return row;
}

async function main() {
  fs.mkdirSync(IMG_DIR, { recursive: true });
  const browser = await chromium.launch({
    headless: process.env.HEADED !== "1",
    args: ["--no-proxy-server", "--enable-unsafe-webgpu", "--ignore-gpu-blocklist"],
  });
  const report = { generatedAt: new Date().toISOString(), base: BASE, userAgent: null, rows: [] };

  for (const cfg of CONFIGS) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    page.on("pageerror", (e) => console.log("  [pageerror]", e.message.slice(0, 200)));
    await context.addInitScript((device) => {
      window.__cfgDevice = device;
    }, cfg.device);
    const url = process.env.THREADS ? `${BASE}?threads=${process.env.THREADS}` : BASE;
    await page.goto(url, { waitUntil: "domcontentloaded" });
    await waitReady(page);
    if (!report.userAgent) report.userAgent = await page.evaluate(() => navigator.userAgent);

    const gpu = await page.evaluate(async () => {
      if (!navigator.gpu) return { ok: false, reason: "no navigator.gpu" };
      try {
        const a = await navigator.gpu.requestAdapter();
        if (!a) return { ok: false, reason: "no adapter" };
        const i = a.info || {};
        return { ok: true, adapter: [i.vendor, i.architecture, i.description].filter(Boolean).join(" ") };
      } catch (e) {
        return { ok: false, reason: e.message };
      }
    });
    console.log(`WebGPU: ${gpu.ok ? gpu.adapter : "不可用 - " + gpu.reason}`);

    // 冷启动：新上下文，模型缓存为空
    await sleep(2000);
    report.rows.push(await runOnce(page, cfg, "cold"));
    await sleep(2000);

    // 热启动：同一上下文刷新页面，模型命中 Cache Storage
    await page.reload({ waitUntil: "domcontentloaded" });
    await waitReady(page);
    report.rows.push(await runOnce(page, cfg, "warm"));

    await context.close();
  }

  await browser.close();
  // 统一在最后清理集合：删集合会触发合并，放在测量之间会污染下一轮耗时
  for (const name of CLEANUP) await api(`/api/collections/${name}`, { method: "DELETE" }).catch(() => {});
  const out = path.join(OUT_DIR, process.env.OUT || "bench-report.json");
  fs.writeFileSync(out, JSON.stringify(report, null, 2));
  console.log(`\n写入 ${out}`);
  console.table(
    report.rows.map((r) => ({
      配置: `${r.device}/${r.dtype}/b${r.batch}/${r.phase}`,
      '模型加载(ms)': r.modelLoadMs,
      '向量化(ms)': r.embedDocsMs,
      '吞吐(docs/s)': r.docsPerSec,
      '写入(ms)': r.seekdbWriteMs,
      '查询向量化(ms)': r.queryEmbedMedianMs,
      'seekdb(ms)': r.seekdbSearchMedianMs,
      'e2e中位数(ms)': r.e2eMedianMs,
    }))
  );
}

main().catch((e) => {
  console.error("基准失败:", e.message);
  process.exit(1);
});
