/**
 * 端侧向量化：Transformers.js（ONNX Runtime Web）+ bge-small-zh-v1.5。
 * 所有推理都在浏览器内完成，服务端只提供静态模型文件与 seekdb 读写。
 */
import { pipeline, env } from "/vendor/transformers.web.min.js";

export const MODEL_ID = "bge-small-zh-v1.5";
export const EMBED_DIM = 512;
/** bge 官方建议的检索查询前缀，只在查询侧拼接。 */
const QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章：";

// 模型与 ORT wasm 全部自托管，运行时不访问任何 CDN / 云端推理接口。
env.allowLocalModels = true;
env.allowRemoteModels = false;
env.localModelPath = "/models/";
env.useBrowserCache = true;
env.backends.onnx.wasm.wasmPaths = "/vendor/ort/";
// 线程数：URL 参数 ?threads=N > localStorage 用户选择 > CPU 核数（上限 4）
const THREADS = (() => {
  const q = Number(new URLSearchParams(location.search).get("threads"));
  if (q >= 1) return q;
  const saved = Number(localStorage.getItem("edgekb-threads"));
  if (saved >= 1) return saved;
  return Math.max(1, Math.min(4, navigator.hardwareConcurrency || 1));
})();
env.backends.onnx.wasm.numThreads = THREADS;
export const WASM_THREADS = THREADS;

/** 探测 WebGPU 是否真正可用（有适配器、能拿到 device 才算）。 */
export async function detectWebGPU() {
  if (!("gpu" in navigator)) return { ok: false, reason: "浏览器没有 navigator.gpu" };
  try {
    const adapter = await navigator.gpu.requestAdapter({ powerPreference: "high-performance" });
    if (!adapter) return { ok: false, reason: "requestAdapter() 返回 null（无可用 GPU 适配器）" };
    await adapter.requestDevice();
    const info = adapter.info || {};
    const name = [info.vendor, info.architecture, info.device, info.description].filter(Boolean).join(" ").trim();
    return { ok: true, adapter: name || "GPU adapter", features: [...(adapter.features || [])] };
  } catch (e) {
    return { ok: false, reason: `WebGPU 初始化失败：${e.message}` };
  }
}

const median = (arr) => {
  const a = [...arr].sort((x, y) => x - y);
  const m = Math.floor(a.length / 2);
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
};

export class Embedder {
  constructor() {
    this.pipe = null;
    this.device = null;
    this.dtype = null;
    this.loadMs = 0;
    this.cacheHit = null;
    this.downloadBytes = { loaded: 0, total: 0 };
    this.warmMs = 0;
    this.embedCalls = [];
  }

  get ready() {
    return Boolean(this.pipe);
  }

  /**
   * 加载模型。device 必须是 "webgpu" 或 "wasm"；dtype 为 int8 / fp16 / fp32。
   * onProgress 收到 transformers.js 的下载/初始化事件。
   */
  async load({ device = "wasm", dtype = "int8", onProgress } = {}) {
    this.pipe = null;
    this.downloadBytes = { loaded: 0, total: 0 };
    const t0 = performance.now();
    this.pipe = await pipeline("feature-extraction", MODEL_ID, {
      device,
      dtype,
      progress_callback: (p) => {
        if (p?.status === "progress" && p.total) {
          this.downloadBytes.total = Math.max(this.downloadBytes.total, p.total);
          this.downloadBytes.loaded = Math.max(this.downloadBytes.loaded, p.loaded || 0);
        }
        onProgress?.(p);
      },
    });
    const tLoaded = performance.now();
    // 预热：把算子编译 / 显存分配等冷启动成本从用户查询里挪走
    await this.pipe(["预热文本"], { pooling: "cls", normalize: true });
    const tWarm = performance.now();
    this.device = device;
    this.dtype = dtype;
    this.loadMs = tLoaded - t0;
    this.warmMs = tWarm - tLoaded;
    return {
      device,
      dtype,
      loadMs: Math.round(this.loadMs),
      warmMs: Math.round(this.warmMs),
      downloadedMB: this.downloadBytes.total ? +(this.downloadBytes.total / 1048576).toFixed(2) : null,
    };
  }

  /**
   * 批量向量化。batchSize 控制单次送入模型的文本条数，
   * onBatch 回传每批耗时，便于把吞吐写进性能面板。
   */
  async embed(texts, { batchSize = 16, isQuery = false, onBatch } = {}) {
    if (!this.pipe) throw new Error("模型尚未加载");
    const prefix = isQuery ? QUERY_PREFIX : "";
    const vectors = [];
    const batchTimes = [];
    const t0 = performance.now();
    for (let i = 0; i < texts.length; i += batchSize) {
      const chunk = texts.slice(i, i + batchSize).map((t) => prefix + t);
      const bt = performance.now();
      const out = await this.pipe(chunk, { pooling: "cls", normalize: true });
      const list = out.tolist();
      for (const v of list) vectors.push(v);
      const ms = performance.now() - bt;
      batchTimes.push({ size: chunk.length, ms: Math.round(ms) });
      onBatch?.({ done: vectors.length, total: texts.length, batchMs: Math.round(ms), batchSize: chunk.length });
    }
    const totalMs = performance.now() - t0;
    this.embedCalls.push({ count: texts.length, ms: Math.round(totalMs) });
    return {
      vectors,
      totalMs,
      batchTimes,
      perDocMs: +(totalMs / Math.max(1, texts.length)).toFixed(2),
      docsPerSec: +(texts.length / (totalMs / 1000)).toFixed(2),
    };
  }

  stats() {
    const ms = this.embedCalls.map((c) => c.ms);
    return { calls: this.embedCalls.length, medianMs: ms.length ? median(ms) : null };
  }
}

export { median };
