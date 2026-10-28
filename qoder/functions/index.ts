/**
 * StudyMind Edge Function for Qoder Sites
 * Integrates PowerContext (memory/context) + seekdb (knowledge retrieval) via HTTP APIs
 */

function getEnv(key: string, fallback: string): string {
  try { return Deno.env.get(key) || fallback; } catch { return fallback; }
}

const POWERCONTEXT_BASE = getEnv('POWERCONTEXT_API_URL', 'http://localhost:8848');
const SCOPE_ID = getEnv('POWERCONTEXT_SCOPE_ID', 'default_learning_scope');
const BACKEND_BASE = getEnv('BACKEND_API_URL', '');

interface Mem { id: string; content: string; family: string; metadata: Record<string, unknown>; ts: string }
interface Mat { id: string; title: string; content: string; subject: string; topic: string; type: string; tags: string[]; ts: string }

const memories: Mem[] = [];
const materials: Mat[] = [];
let memCounter = 0;
let matCounter = 0;

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function normalizePath(rawPath: string): string {
  const m = rawPath.match(/^\/app(?:-[a-z0-9]+)?(\/.*)?$/);
  if (m) return m[1] || "/";
  if (rawPath.startsWith("/functions/v1/app/")) return "/" + rawPath.slice("/functions/v1/app/".length);
  if (rawPath === "/functions/v1/app") return "/";
  return rawPath;
}

// ---- PowerContext API calls ----

async function pcSaveMemory(content: string, family: string, metadata: Record<string, unknown> = {}): Promise<unknown> {
  try {
    const resp = await fetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/memories`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE_ID },
      body: JSON.stringify({ content, artifact_type: "memory", family, metadata, source: { type: "journal", entry: new Date().toISOString() } }),
    });
    if (resp.ok) return await resp.json();
  } catch { /* fallback below */ }
  const entry: Mem = { id: `mem_${++memCounter}`, content, family, metadata, ts: new Date().toISOString() };
  memories.push(entry);
  return entry;
}

async function pcRecall(query: string, family?: string, limit = 10): Promise<Mem[]> {
  try {
    const params = new URLSearchParams({ query, limit: String(limit) });
    if (family) params.set("family", family);
    const resp = await fetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/memories/recall?${params}`, {
      headers: { "X-Scope-Id": SCOPE_ID },
    });
    if (resp.ok) { const d = await resp.json(); return Array.isArray(d) ? d : (d.memories || []); }
  } catch { /* fallback below */ }
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  let results = family ? memories.filter(m => m.family === family) : [...memories];
  return results.map(m => ({ score: words.reduce((a, w) => a + (m.content.toLowerCase().includes(w) ? 1 : 0), 0), item: m }))
    .sort((a, b) => b.score - a.score).slice(0, limit).map(s => s.item);
}

async function pcSaveExperience(situation: string, action: string, outcome: string, lesson: string): Promise<unknown> {
  try {
    const resp = await fetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/experiences`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE_ID },
      body: JSON.stringify({ artifact_type: "experience", content: JSON.stringify({ situation, action, outcome, lesson, timestamp: new Date().toISOString() }), metadata: { subject: situation.slice(0, 50), tags: ["learning_experience"] } }),
    });
    if (resp.ok) return await resp.json();
  } catch { /* ignore */ }
  return { status: "fallback" };
}

async function pcPreparedContext(query: string, budget = 4000): Promise<unknown> {
  try {
    const resp = await fetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/recall/prepared-context`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE_ID },
      body: JSON.stringify({ query, budget }),
    });
    if (resp.ok) return await resp.json();
  } catch { /* fallback */ }
  return { memories: await pcRecall(query, undefined, 10), budget };
}

async function pcCreateHandoff(desc: string, steps: string[]): Promise<unknown> {
  try {
    const resp = await fetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/handoffs`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE_ID },
      body: JSON.stringify({ artifact_type: "handoff", content: JSON.stringify({ work_description: desc, next_steps: steps, timestamp: new Date().toISOString() }), metadata: { type: "study_session_handoff" } }),
    });
    if (resp.ok) return await resp.json();
  } catch { /* ignore */ }
  return { status: "fallback" };
}

// ---- seekdb via backend proxy ----

async function seekdbAdd(title: string, content: string, subject: string, topic: string, type = "note", tags: string[] = []): Promise<{ id: string; status: string }> {
  if (BACKEND_BASE) {
    try {
      const resp = await fetch(`${BACKEND_BASE}/api/materials`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, content, subject, topic, type, tags }) });
      if (resp.ok) return await resp.json();
    } catch { /* fallback */ }
  }
  const entry: Mat = { id: `mat_${++matCounter}`, title, content, subject, topic, type, tags, ts: new Date().toISOString() };
  materials.push(entry);
  return { id: entry.id, status: "stored_in_memory" };
}

async function seekdbSearch(query: string, subject?: string | null, limit = 5): Promise<Array<{ content: string; metadata: Record<string, unknown>; score: number }>> {
  if (BACKEND_BASE) {
    try {
      const params = new URLSearchParams({ q: query, limit: String(limit) });
      if (subject) params.set("subject", subject);
      const resp = await fetch(`${BACKEND_BASE}/api/materials/search?${params}`);
      if (resp.ok) { const d = await resp.json(); return d.results || []; }
    } catch { /* fallback */ }
  }
  const tokens = query.toLowerCase().split(/[\s,，、。]+/).filter(Boolean);
  let filtered = subject ? materials.filter(m => m.subject === subject) : [...materials];
  return filtered.map(m => { const t = `${m.title} ${m.content} ${m.subject} ${m.topic}`.toLowerCase(); const s = tokens.reduce((a, w) => a + (t.includes(w) ? 1 : 0), 0); return { score: s, item: m }; })
    .sort((a, b) => b.score - a.score).filter(s => s.score > 0).slice(0, limit)
    .map(s => ({ content: s.item.content, metadata: { title: s.item.title, subject: s.item.subject, topic: s.item.topic, type: s.item.type, tags: s.item.tags }, score: 1 - s.score / (tokens.length + 1) }));
}

async function seekdbList(subject?: string | null): Promise<Mat[]> {
  if (BACKEND_BASE && subject) {
    try { const resp = await fetch(`${BACKEND_BASE}/api/materials?subject=${encodeURIComponent(subject)}`); if (resp.ok) { const d = await resp.json(); return d.materials || []; } } catch { /* fallback */ }
  }
  return subject ? materials.filter(m => m.subject === subject) : [...materials];
}

function loadSamples(): Array<{ id: string; status: string }> {
  const samples: Array<[string, string, string, string, string, string]> = [
    ["Python 列表推导式", "列表推导式是Python中简洁的列表创建方式。语法为[expression for item in iterable if condition]。例如[x**2 for x in range(10) if x%2==0]生成偶数平方列表[0,4,16,36,64]。", "Python编程", "列表与数据结构", "concept", "python,列表,推导式"],
    ["机器学习-监督学习概述", "监督学习使用标注数据训练模型。训练数据包含输入特征和对应标签。常见任务包括分类和回归。算法有线性回归、决策树、SVM、神经网络。", "机器学习", "监督学习基础", "note", "ML,监督学习,分类,回归"],
    ["数据库索引原理", "数据库索引是一种数据结构，用于加速查询。常见类型：B+树索引适合范围查询，哈希索引适合等值查询，全文索引适合文本搜索。", "数据库系统", "索引与查询优化", "concept", "数据库,索引,B+树,性能优化"],
    ["HTTP状态码总结", "2xx成功：200 OK, 201 Created。3xx重定向：301永久, 302临时, 304 Not Modified。4xx客户端错误：400, 401, 403, 404。5xx服务端错误：500, 502, 503。", "计算机网络", "HTTP协议", "note", "HTTP,网络,状态码"],
    ["向量检索与语义搜索", "向量检索将文本转换为高维向量，利用余弦相似度衡量语义相关性。流程：1)Embedding向量化 2)存入向量数据库 3)查询时计算相似度排序。应用于RAG、推荐系统。", "数据库系统", "向量数据库", "concept", "向量检索,语义搜索,Embedding,RAG"],
  ];
  return samples.map(([title, content, subject, topic, type, tagsStr]) => {
    const tags = tagsStr.split(",");
    materials.push({ id: `mat_${++matCounter}`, title, content, subject, topic, type, tags, ts: new Date().toISOString() });
    memories.push({ id: `mem_${++memCounter}`, content: `Sample: ${title} (${subject})`, family: "topic", metadata: { subject, title }, ts: new Date().toISOString() });
    return { id: `mat_${matCounter}`, status: "stored" };
  });
}

async function handler(request: Request): Promise<Response> {
  const method = request.method;
  const url = new URL(request.url);
  const path = normalizePath(url.pathname);

  if (method === "OPTIONS") {
    return new Response(null, { headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type" } });
  }

  try {
    // Health
    if (path === "/health" && method === "GET") {
      let pcOk = false;
      try { const r = await fetch(`${POWERCONTEXT_BASE}/api/v1/system/health`); pcOk = r.status === 200; } catch { /* */ }
      return json({ status: "ok", powercontext: pcOk, seekdb: true, mode: BACKEND_BASE ? "backend-proxy" : "in-memory-fallback", timestamp: new Date().toISOString() });
    }

    // Materials POST
    if (path === "/materials" && method === "POST") {
      const body = await request.json() as Record<string, string | string[]>;
      if (!body.title || !body.content || !body.subject || !body.topic) return json({ error: "Missing required fields" }, 400);
      const result = await seekdbAdd(body.title as string, body.content as string, body.subject as string, body.topic as string, (body.type as string) || "note", (body.tags as string[]) || []);
      await pcSaveMemory(`Material: ${body.title} (${body.subject})`, "topic", { subject: body.subject, title: body.title });
      return json(result, 201);
    }

    // Materials GET
    if (path === "/materials" && method === "GET") {
      const list = await seekdbList(url.searchParams.get("subject") || undefined);
      return json({ materials: list, count: list.length });
    }

    // Search
    if (path === "/materials/search" && method === "GET") {
      const q = url.searchParams.get("q");
      if (!q) return json({ error: "Query parameter q is required" }, 400);
      const results = await seekdbSearch(q, url.searchParams.get("subject") || undefined, parseInt(url.searchParams.get("limit") || "5"));
      await pcSaveMemory(`Search: "${q}", ${results.length} results`, "insight", { query: q, result_count: results.length });
      return json({ query: q, count: results.length, results });
    }

    // Progress POST
    if (path === "/progress" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      if (!body.subject || !body.topic || body.progress === undefined) return json({ error: "Missing: subject, topic, progress" }, 400);
      const result = await pcSaveMemory(`Progress: ${body.subject} > ${body.topic} = ${body.progress}%. ${body.notes || ""}`, "topic", { subject: body.subject, topic: body.topic, progress: body.progress });
      return json(result, 201);
    }

    // Progress summary
    if (path === "/progress/summary" && method === "GET") {
      const subject = url.searchParams.get("subject");
      const matched = await pcRecall(subject ? `progress ${subject}` : "all progress");
      return json({ total_memories: matched.length, memories: matched, subject });
    }

    // Weak points
    if (path === "/weak-points" && method === "POST") {
      const body = await request.json() as Record<string, string>;
      if (!body.subject || !body.topic || !body.description) return json({ error: "Missing: subject, topic, description" }, 400);
      const result = await pcSaveMemory(`Weak point: ${body.subject} > ${body.topic} - ${body.description}`, "insight", { type: "weak_point", subject: body.subject, topic: body.topic });
      return json(result, 201);
    }

    // Recommendations
    if (path === "/recommendations" && method === "GET") {
      const subject = url.searchParams.get("subject") || "";
      const mems = await pcRecall(`study progress ${subject}`);
      const mats = await seekdbSearch(subject || "study");
      await pcSaveExperience(`Recommendations for ${subject || "general"}`, "Generated recommendations", `Found ${mats.length} materials`, `Focus: ${subject || "multiple"}`);
      return json({ based_on_memories: mems.length, suggested_materials: mats.slice(0, 3), focus_areas: mems.filter(m => m.content.toLowerCase().includes("weak")).map(m => m.content), next_steps: mems.filter(m => m.content.toLowerCase().includes("progress")).map(m => m.content) });
    }

    // Quiz
    if (path === "/quiz/generate" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      const query = `${(body.subject as string) || ""} ${(body.topic as string) || ""}`.trim() || "study";
      const mats = await seekdbSearch(query, undefined, ((body.count as number) || 5) * 2);
      if (!mats.length) return json({ error: "No materials found", quiz: null }, 404);
      const count = Math.min((body.count as number) || 5, mats.length);
      await pcSaveExperience(`Quiz for ${body.subject} > ${body.topic}`, `Generated ${count} questions`, "Quiz created", `Testing: ${body.topic}`);
      return json({ subject: body.subject || "", topic: body.topic || "", generated_at: new Date().toISOString(), questions: mats.slice(0, count).map((m, i) => ({ id: i + 1, type: "open_ended", prompt: `Explain: ${m.content.substring(0, 200)}...`, source_material: (m.metadata as Record<string, string>).title || "Unknown" })) });
    }

    // Session start
    if (path === "/session/start" && method === "POST") {
      const body = await request.json() as Record<string, string>;
      const context = await pcPreparedContext(`study ${body.subject || "General"} ${body.goal || ""}`);
      await pcCreateHandoff(`Starting session: ${body.subject || "General"}`, body.goal ? [body.goal] : ["Review weak points"]);
      return json({ session_id: Date.now().toString(), subject: body.subject || "General", goal: body.goal || "", context, started_at: new Date().toISOString() });
    }

    // Session end
    if (path === "/session/end" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      await pcSaveMemory(`Session ${body.session_id}: ${body.summary}`, "topic", { session_id: body.session_id });
      await pcSaveExperience(`Session ${body.session_id} completed`, "Session ended", String(body.summary || ""), "Session saved");
      return json({ status: "saved", session_id: body.session_id });
    }

    // Ask
    if (path === "/ask" && method === "POST") {
      const body = await request.json() as Record<string, string>;
      if (!body.question) return json({ error: "Question required" }, 400);
      const memResults = await pcRecall(body.question);
      const matResults = await seekdbSearch(body.question);
      const prepared = await pcPreparedContext(body.question);
      const parts: string[] = [];
      if (matResults.length) { parts.push("Based on your study materials:\n"); matResults.slice(0, 3).forEach((m, i) => { parts.push(`  [${i + 1}] From "${(m.metadata as Record<string, string>).title || "Unknown"}": ${m.content.substring(0, 200)}...\n`); }); }
      if (memResults.length) { parts.push("\nFrom your learning history:\n"); memResults.slice(0, 3).forEach(m => { parts.push(`  - ${m.content.substring(0, 150)}\n`); }); }
      if (!parts.length) parts.push("No relevant materials found.");
      const topics = new Set<string>();
      memResults.forEach(m => { const meta = m.metadata as Record<string, string>; if (meta.subject) topics.add(meta.subject); if (meta.topic) topics.add(meta.topic); });
      matResults.forEach(m => { const meta = m.metadata as Record<string, string>; if (meta.subject) topics.add(meta.subject); if (meta.topic) topics.add(meta.topic); });
      await pcSaveExperience(`User asked: ${body.question.slice(0, 100)}`, "Searched PowerContext + seekdb", `Found ${memResults.length} memories, ${matResults.length} materials`, parts.join("").slice(0, 200));
      return json({ question: body.question, context_sources: { memories: memResults, materials: matResults, prepared_context: prepared }, answer: parts.join(""), related_topics: Array.from(topics) });
    }

    // Load samples
    if (path === "/load-samples" && method === "POST") {
      const results = loadSamples();
      for (const r of results) { const mat = materials.find(m => m.id === r.id); if (mat) await pcSaveMemory(`Sample: ${mat.title} (${mat.subject})`, "topic", { subject: mat.subject, title: mat.title }); }
      return json({ status: "loaded", count: results.length, materials: results });
    }

    return json({ error: `Not found: ${method} ${path}` }, 404);
  } catch (e) {
    return json({ error: `Internal error: ${(e as Error).message}` }, 500);
  }
}

Deno.serve(handler);
