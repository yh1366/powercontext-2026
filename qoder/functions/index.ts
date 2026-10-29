/**
 * StudyMind Edge Function - PowerContext + seekdb integration
 * All external API calls have 2s timeout + in-memory fallback
 */

function getEnv(key: string, fallback: string): string {
  try { return Deno.env.get(key) || fallback; } catch { return fallback; }
}

const PC_BASE = getEnv('POWERCONTEXT_API_URL', 'http://localhost:8848');
const SCOPE = getEnv('POWERCONTEXT_SCOPE_ID', 'default_learning_scope');

interface Mem { id: string; content: string; family: string; metadata: Record<string, unknown>; ts: string }
interface Mat { id: string; title: string; content: string; subject: string; topic: string; type: string; tags: string[]; ts: string }

const memStore: Mem[] = [];
const matStore: Mat[] = [];
let memId = 0, matId = 0;

function ok(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*" },
  });
}

function parsePath(raw: string): string {
  const m = raw.match(/^\/app(?:-[a-z0-9]+)?(\/.*)?$/);
  if (m) return m[1] || "/";
  if (raw.startsWith("/functions/v1/app/")) return "/" + raw.slice("/functions/v1/app/".length);
  if (raw === "/functions/v1/app") return "/";
  return raw;
}

async function timedFetch(url: string, opts: RequestInit = {}): Promise<Response | null> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 2000);
  try { return await fetch(url, { ...opts, signal: ctrl.signal }); }
  catch { return null; }
  finally { clearTimeout(t); }
}

// ---- PowerContext: Memory ----

async function saveMemory(content: string, family: string, metadata: Record<string, unknown> = {}): Promise<Mem> {
  const entry: Mem = { id: `mem_${++memId}`, content, family, metadata, ts: new Date().toISOString() };
  try {
    const r = await timedFetch(`${PC_BASE}/api/v1/scopes/${SCOPE}/memories`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE },
      body: JSON.stringify({ content, artifact_type: "memory", family, metadata, source: { type: "journal", entry: entry.ts } }),
    });
    if (r && r.ok) {
      const d = await r.json().catch(() => ({}));
      entry.id = d.artifact_id || entry.id;
    }
  } catch { /* fallback */ }
  memStore.push(entry);
  return entry;
}

async function recallMemories(query: string, family?: string, limit = 10): Promise<Mem[]> {
  try {
    const p = new URLSearchParams({ query, limit: String(limit) });
    if (family) p.set("family", family);
    const r = await timedFetch(`${PC_BASE}/api/v1/scopes/${SCOPE}/memories/recall?${p}`, {
      headers: { "X-Scope-Id": SCOPE },
    });
    if (r && r.ok) {
      const d = await r.json().catch(() => []);
      const arr = Array.isArray(d) ? d : (d.memories || d.data || d.results || []);
      if (Array.isArray(arr) && arr.length > 0) return arr;
    }
  } catch { /* fallback */ }
  const words = tokenize(query);
  let items = family ? memStore.filter(m => m.family === family) : [...memStore];
  return items.map(m => ({
    s: words.reduce((a, w) => a + (m.content.toLowerCase().includes(w) ? 1 : 0), 0), m
  })).sort((a, b) => b.s - a.s).slice(0, limit).map(x => x.m);
}

async function saveExperience(situation: string, action: string, outcome: string, lesson: string): Promise<void> {
  try {
    await timedFetch(`${PC_BASE}/api/v1/scopes/${SCOPE}/experiences`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE },
      body: JSON.stringify({
        artifact_type: "experience",
        content: JSON.stringify({ situation, action, outcome, lesson, ts: new Date().toISOString() }),
        metadata: { subject: situation.slice(0, 50), tags: ["learning_experience"] }
      }),
    });
  } catch { /* ignore */ }
  memStore.push({ id: `mem_${++memId}`, content: `${situation}: ${action}`, family: "experience", metadata: { situation, action, outcome, lesson }, ts: new Date().toISOString() });
}

async function preparedContext(query: string): Promise<unknown> {
  try {
    const r = await timedFetch(`${PC_BASE}/api/v1/scopes/${SCOPE}/recall/prepared-context`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE },
      body: JSON.stringify({ query, budget: 4000 }),
    });
    if (r && r.ok) return await r.json().catch(() => ({}));
  } catch { /* fallback */ }
  return { memories: await recallMemories(query, undefined, 10), budget: 4000 };
}

async function createHandoff(desc: string, steps: string[]): Promise<void> {
  try {
    await timedFetch(`${PC_BASE}/api/v1/scopes/${SCOPE}/handoffs`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE },
      body: JSON.stringify({
        artifact_type: "handoff",
        content: JSON.stringify({ work_description: desc, next_steps: steps, ts: new Date().toISOString() }),
        metadata: { type: "study_session_handoff" }
      }),
    });
  } catch { /* ignore */ }
}

// ---- seekdb: Materials (in-memory with API attempt) ----

function addMaterial(title: string, content: string, subject: string, topic: string, type = "note", tags: string[] = []): Mat {
  const mat: Mat = { id: `mat_${++matId}`, title, content, subject, topic, type, tags, ts: new Date().toISOString() };
  matStore.push(mat);
  return mat;
}

// Chinese-aware tokenizer: CJK runs longer than 2 chars are split into
// overlapping bigrams so partial phrases (e.g. "推导式" inside
// "列表推导式") still match. Mirrors qoder/backend/seekdb_client.py.
function tokenize(q: string): string[] {
  const raw = q.toLowerCase().split(/[\s,，、。；;？?！!]+/).filter(Boolean);
  const tokens: string[] = [];
  for (const w of raw) {
    if (/[\u4e00-\u9fff]/.test(w) && w.length > 2) {
      for (let i = 0; i + 2 <= w.length; i++) tokens.push(w.slice(i, i + 2));
    } else if (w) {
      tokens.push(w);
    }
  }
  return tokens;
}

function searchMaterials(query: string, subject?: string, limit = 5): Array<{ content: string; metadata: Record<string, unknown>; score: number }> {
  const tokens = tokenize(query);
  let items = subject ? matStore.filter(m => m.subject === subject) : [...matStore];
  return items.map(m => {
    const text = `${m.title} ${m.content} ${m.subject} ${m.topic}`.toLowerCase();
    const score = tokens.reduce((a, w) => a + (text.includes(w) ? 1 : 0), 0);
    return { score, item: m };
  }).sort((a, b) => b.score - a.score).filter(x => x.score > 0).slice(0, limit)
    .map(x => ({
      content: x.item.content,
      metadata: { title: x.item.title, subject: x.item.subject, topic: x.item.topic, type: x.item.type, tags: x.item.tags },
      // Higher = more relevant, same convention as the Flask backend.
      score: tokens.length ? Math.round((x.score / tokens.length) * 10000) / 10000 : 0
    }));
}

// ---- Sample data ----

function loadSamples() {
  if (matStore.length > 0) return;
  const samples: Array<[string, string, string, string, string, string]> = [
    ["Python 列表推导式", "列表推导式是Python中简洁的列表创建方式。语法为[expression for item in iterable if condition]。例如[x**2 for x in range(10) if x%2==0]生成偶数平方列表[0,4,16,36,64]。", "Python编程", "列表与数据结构", "concept", "python,列表,推导式"],
    ["机器学习-监督学习概述", "监督学习使用标注数据训练模型。训练数据包含输入特征和对应标签。常见任务包括分类和回归。算法有线性回归、决策树、SVM、神经网络。", "机器学习", "监督学习基础", "note", "ML,监督学习,分类,回归"],
    ["数据库索引原理", "数据库索引是一种数据结构，用于加速查询。常见类型：B+树索引适合范围查询，哈希索引适合等值查询，全文索引适合文本搜索。", "数据库系统", "索引与查询优化", "concept", "数据库,索引,B+树,性能优化"],
    ["HTTP状态码总结", "2xx成功：200 OK, 201 Created。3xx重定向：301永久, 302临时, 304 Not Modified。4xx客户端错误：400, 401, 403, 404。5xx服务端错误：500, 502, 503。", "计算机网络", "HTTP协议", "note", "HTTP,网络,状态码"],
    ["向量检索与语义搜索", "向量检索将文本转换为高维向量，利用余弦相似度衡量语义相关性。流程：1)Embedding向量化 2)存入向量数据库 3)查询时计算相似度排序。应用于RAG、推荐系统。", "数据库系统", "向量数据库", "concept", "向量检索,语义搜索,Embedding,RAG"],
  ];
  for (const [title, content, subject, topic, type, tagsStr] of samples) {
    addMaterial(title, content, subject, topic, type, tagsStr.split(","));
    saveMemory(`Study material: ${title} (${subject} > ${topic})`, "topic", { subject, topic, title });
  }
}

// ---- Handler ----

async function handler(request: Request): Promise<Response> {
  try {
    const method = request.method;
    const url = new URL(request.url);
    const path = parsePath(url.pathname);

    if (method === "OPTIONS") {
      return new Response(null, { headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type" } });
    }

    if (path === "/health" && method === "GET") {
      // Report only what is actually reachable from this edge runtime.
      // - PowerContext: probe its ready endpoint directly.
      // - seekdb speaks the MySQL protocol, which edge functions cannot speak,
      //   so it counts as live only when an HTTP backend bridge reports it.
      // Never hardcode a connected/integrated state.
      let pcLive = false;
      try {
        const r = await timedFetch(`${PC_BASE}/health/ready`);
        pcLive = !!(r && r.ok);
      } catch { pcLive = false; }

      let seekdbLive = false;
      let seekdbBackend = "memory-fallback";
      const backendUrl = getEnv("BACKEND_API_URL", "").replace(/\/+$/, "");
      if (backendUrl) {
        try {
          const r = await timedFetch(`${backendUrl}/api/health`);
          if (r && r.ok) {
            const d = await r.json().catch(() => ({} as Record<string, unknown>));
            seekdbLive = (d as Record<string, unknown>).seekdb === true;
            seekdbBackend = String((d as Record<string, unknown>).seekdb_backend || "remote-backend");
          }
        } catch { /* backend unreachable -> keep real state */ }
      }

      return ok({
        status: "ok",
        powercontext: pcLive,
        seekdb: seekdbLive,
        mode: pcLive && seekdbLive ? "full" : "degraded",
        seekdb_backend: seekdbBackend,
        powercontext_backend: pcLive ? "powercontext" : "memory-fallback",
        material_count: matStore.length,
        note: backendUrl
          ? "Probing BACKEND_API_URL for the real seekdb / PowerContext state."
          : "Qoder Sites edge runtime: data lives in function memory. Set BACKEND_API_URL to bridge a real backend, or run qoder/backend locally.",
        timestamp: new Date().toISOString(),
      });
    }

    if (path === "/materials" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      if (!body.title || !body.content || !body.subject || !body.topic) {
        return ok({ error: "Missing: title, content, subject, topic" }, 400);
      }
      const mat = addMaterial(String(body.title), String(body.content), String(body.subject), String(body.topic), String(body.type || "note"), (body.tags as string[]) || []);
      await saveMemory(`Material added: ${body.title} (${body.subject})`, "topic", { subject: body.subject, title: body.title, material_id: mat.id });
      return ok({ id: mat.id, status: "stored", title: mat.title, subject: mat.subject }, 201);
    }

    if (path === "/materials" && method === "GET") {
      const subject = url.searchParams.get("subject");
      const list = subject ? matStore.filter(m => m.subject === subject) : [...matStore];
      return ok({ materials: list, count: list.length });
    }

    if (path === "/materials/search" && method === "GET") {
      const q = url.searchParams.get("q");
      if (!q) return ok({ error: "Query parameter q required" }, 400);
      const results = searchMaterials(q, url.searchParams.get("subject") || undefined, parseInt(url.searchParams.get("limit") || "5"));
      await saveMemory(`Search: "${q}", found ${results.length} results`, "insight", { query: q, result_count: results.length });
      return ok({ query: q, count: results.length, results });
    }

    if (path === "/progress" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      if (!body.subject || !body.topic || body.progress === undefined) {
        return ok({ error: "Missing: subject, topic, progress" }, 400);
      }
      const result = await saveMemory(
        `Progress: ${body.subject} > ${body.topic} = ${body.progress}%. ${body.notes || ""}`,
        "topic",
        { subject: body.subject, topic: body.topic, progress: body.progress }
      );
      return ok(result, 201);
    }

    if (path === "/progress/summary" && method === "GET") {
      const subject = url.searchParams.get("subject");
      const matched = await recallMemories(subject ? `progress ${subject}` : "all learning progress");
      return ok({ total_memories: matched.length, memories: matched, subject });
    }

    if (path === "/weak-points" && method === "POST") {
      const body = await request.json() as Record<string, string>;
      if (!body.subject || !body.topic || !body.description) {
        return ok({ error: "Missing: subject, topic, description" }, 400);
      }
      const result = await saveMemory(
        `Weak point: ${body.subject} > ${body.topic} - ${body.description}`,
        "insight",
        { type: "weak_point", subject: body.subject, topic: body.topic }
      );
      return ok(result, 201);
    }

    if (path === "/recommendations" && method === "GET") {
      const subject = url.searchParams.get("subject") || "";
      const mems = await recallMemories(`study progress weak points ${subject}`);
      const mats = searchMaterials(subject || "study");
      await saveExperience(`Recommendations for ${subject || "general"}`, "Generated recommendations", `Found ${mats.length} materials`, `Focus: ${subject || "multiple"}`);
      return ok({
        based_on_memories: mems.length,
        suggested_materials: mats.slice(0, 3),
        focus_areas: mems.filter(m => m.content.toLowerCase().includes("weak")).map(m => m.content),
        next_steps: mems.filter(m => m.content.toLowerCase().includes("progress")).map(m => m.content),
      });
    }

    if (path === "/quiz/generate" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      const query = `${body.subject || ""} ${body.topic || ""}`.trim() || "study";
      const mats = searchMaterials(query, undefined, ((body.count as number) || 5) * 2);
      if (!mats.length) return ok({ error: "No materials found", quiz: null }, 404);
      const count = Math.min((body.count as number) || 5, mats.length);
      await saveExperience(`Quiz for ${body.subject} > ${body.topic}`, `Generated ${count} questions`, "Quiz created", `Testing: ${body.topic}`);
      return ok({
        subject: body.subject || "", topic: String(body.topic || ""),
        generated_at: new Date().toISOString(),
        questions: mats.slice(0, count).map((m, i) => ({
          id: i + 1, type: "open_ended",
          prompt: `Explain: ${m.content.substring(0, 200)}...`,
          source_material: (m.metadata as Record<string, string>).title || "Unknown",
        })),
      });
    }

    if (path === "/session/start" && method === "POST") {
      const body = await request.json() as Record<string, string>;
      const context = await preparedContext(`study ${body.subject || "General"} ${body.goal || ""}`);
      await createHandoff(`Starting session: ${body.subject || "General"}`, body.goal ? [body.goal] : ["Review weak points"]);
      return ok({ session_id: Date.now().toString(), subject: body.subject || "General", goal: body.goal || "", context, started_at: new Date().toISOString() });
    }

    if (path === "/session/end" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      await saveMemory(`Session ${body.session_id}: ${body.summary}`, "topic", { session_id: body.session_id });
      await saveExperience(`Session ${body.session_id} completed`, "Session ended", String(body.summary || ""), "Session saved");
      return ok({ status: "saved", session_id: body.session_id });
    }

    if (path === "/ask" && method === "POST") {
      const body = await request.json() as Record<string, string>;
      if (!body.question) return ok({ error: "Question required" }, 400);
      const memResults = await recallMemories(body.question);
      const matResults = searchMaterials(body.question);
      const prepared = await preparedContext(body.question);
      const parts: string[] = [];
      if (matResults.length) {
        parts.push("Based on your study materials:\n");
        matResults.slice(0, 3).forEach((m, i) => {
          parts.push(`  [${i + 1}] From "${(m.metadata as Record<string, string>).title || "Unknown"}": ${m.content.substring(0, 200)}...\n`);
        });
      }
      if (memResults.length) {
        parts.push("\nFrom your learning history:\n");
        memResults.slice(0, 3).forEach(m => { parts.push(`  - ${m.content.substring(0, 150)}\n`); });
      }
      if (!parts.length) parts.push("No relevant materials found. Try adding some study materials first!");
      const topics = new Set<string>();
      memResults.forEach(m => { const meta = m.metadata as Record<string, string>; if (meta.subject) topics.add(meta.subject); if (meta.topic) topics.add(meta.topic); });
      matResults.forEach(m => { const meta = m.metadata as Record<string, string>; if (meta.subject) topics.add(meta.subject); if (meta.topic) topics.add(meta.topic); });
      await saveExperience(`User asked: ${body.question.slice(0, 100)}`, "Searched PowerContext + seekdb", `Found ${memResults.length} memories, ${matResults.length} materials`, parts.join("").slice(0, 200));
      return ok({
        question: body.question,
        context_sources: { memories: memResults, materials: matResults, prepared_context: prepared },
        answer: parts.join(""),
        related_topics: Array.from(topics),
      });
    }

    if (path === "/load-samples" && method === "POST") {
      const before = matStore.length;
      loadSamples();
      const added = matStore.length - before;
      return ok({ status: "loaded", count: added || matStore.length, materials: matStore.map(m => ({ id: m.id, title: m.title, status: "stored" })) });
    }

    return ok({ error: `Not found: ${method} ${path}`, routes: ["/health", "/materials", "/materials/search", "/progress", "/progress/summary", "/weak-points", "/recommendations", "/quiz/generate", "/session/start", "/session/end", "/ask", "/load-samples"] }, 404);
  } catch (e) {
    return ok({ error: "Internal server error", detail: (e as Error).message }, 500);
  }
}

loadSamples();
Deno.serve(handler);
