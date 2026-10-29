/**
 * 汇总 docs/bench-*.json（多轮中位数）与 docs/eval-report.json，输出 markdown 表格。
 *   node tools/summarize.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DOCS = path.join(ROOT, "docs");
const median = (a) => {
  const s = [...a].sort((x, y) => x - y);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const r1 = (n) => (n == null ? "—" : Math.round(n));

const files = fs.readdirSync(DOCS).filter((f) => /^bench-.*\.json$/.test(f));
const groups = new Map();
for (const f of files) {
  const rep = JSON.parse(fs.readFileSync(path.join(DOCS, f), "utf8"));
  for (const row of rep.rows) {
    const key = `${row.device}|${row.dtype}|b${row.batch}|t${row.threads ?? "auto"}|${row.phase}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ ...row, file: f });
  }
}

const metrics = [
  ["modelLoadMs", "模型加载"],
  ["embedDocsMs", "文档向量化"],
  ["docsPerSec", "吞吐 docs/s"],
  ["seekdbWriteMs", "写入 seekdb"],
  ["refreshMs", "刷新自检"],
  ["queryEmbedMedianMs", "查询向量化"],
  ["seekdbSearchMedianMs", "seekdb 检索"],
  ["e2eMedianMs", "端到端中位数"],
];

console.log("| 配置 | 轮次 | " + metrics.map((m) => m[1]).join(" | ") + " |");
console.log("|---|---|" + metrics.map(() => "---").join("|") + "|");
for (const [key, rows] of [...groups.entries()].sort()) {
  const [device, dtype, batch, threads, phase] = key.split("|");
  const cells = metrics.map(([k]) => {
    const vals = rows.map((r) => r[k]).filter((v) => v != null);
    if (!vals.length) return "—";
    return k === "docsPerSec" ? median(vals).toFixed(1) : r1(median(vals));
  });
  console.log(`| ${device}/${dtype}/${batch}/${phase} | ${rows.length} | ${cells.join(" | ")} |`);
}

const evalPath = path.join(DOCS, "eval-report.json");
if (fs.existsSync(evalPath)) {
  const rep = JSON.parse(fs.readFileSync(evalPath, "utf8"));
  console.log("\n| 精度 | 模型体积 | 文档向量化 | 吞吐 | 密集 Recall@5 | 混合 Recall@5 | 密集 nDCG@5 | 混合 nDCG@5 |");
  console.log("|---|---|---|---|---|---|---|---|");
  for (const run of rep.runs) {
    const size = run.downloadedMB ? `${run.downloadedMB} MB` : "—";
    const m = run.metrics || {};
    console.log(
      `| ${run.dtype} | ${size} | ${r1(run.embedDocsMs)} ms | ${run.docsPerSec} docs/s | ${m.vector?.recall5 ?? "—"} | ${m.hybrid?.recall5 ?? "—"} | ${m.vector?.ndcg5 ?? "—"} | ${m.hybrid?.ndcg5 ?? "—"} |`
    );
  }
}
