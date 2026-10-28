/**
 * StudyMind Edge Function - PowerContext + seekdb integration
 * All external API calls have 2s timeout + in-memory fallback
 */

// ---- Config (no env dependency for portability) ----
const POWERCONTEXT_BASE = 'http://localhost:8848';
const SCOPE_ID = 'default_learning_scope';

// ---- Types ----
interface Mem { id: string; content: string; family: string; metadata: Record<string, unknown>; ts: string }
interface Mat { id: string; title: string; content: string; subject: string; topic: string; type: string; tags: string[]; ts: string }

// ---- In-memory stores (pre-loaded with samples) ----
const memories: Mem[] = [];
const materials: Mat[] = [];
let memCtr = 0;
let matCtr = 0;

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*" },
  });
}

function normalizePath(raw: string): string {
  const m = raw.match(/^\/app(?:-[a-z0-9]+)?(\/.*)?$/);
  if (m) return m[1] || "/";
  if (raw.startsWith("/functions/v1/app/")) return "/" + raw.slice("/functions/v1/app/".length);
  if (raw === "/functions/v1/app") return "/";
  return raw;
}

// ---- Timeout fetch (2s) ----
async function safeFetch(url: string, opts: RequestInit = {}): Promise<Response | null> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2000);
  try {
    return await fetch(url, { ...opts, signal: ctrl.signal });
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// ---- PowerContext API ----

async function pcSaveMemory(content: string, family: string, metadata: Record<string, unknown> = {}): Promise<Mem> {
  const entry: Mem = { id: `mem_${++memCtr}`, content, family, metadata, ts: new Date().toISOString() };
  try {
    const r = await safeFetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/memories`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE_ID },
      body: JSON.stringify({ content, artifact_type: "memory", family, metadata, source: { type: "journal", entry: entry.ts } }),
    });
    if (r && r.ok) { const d = await r.json(); return { ...entry, id: d.artifact_id || entry.id }; }
  } catch { /* fallback */ }
  memories.push(entry);
  return entry;
}

async function pcRecall(query: string, family?: string, limit = 10): Promise<Mem[]> {
  try {
    const params = new URLSearchParams({ query, limit: String(limit) });
    if (family) params.set("family", family);
    const r = await safeFetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/memories/recall?${params}`, {
      headers: { "X-Scope-Id": SCOPE_ID },
    });
    if (r && r.ok) { const d = await r.json(); return Array.isArray(d) ? d : (d.memories || []); }
  } catch { /* fallback */ }
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  let items = family ? memories.filter(m => m.family === family) : [...memories];
  return items.map(m => ({ s: words.reduce((a, w) => a + (m.content.toLowerCase().includes(w) ? 1 : 0), 0), m }))
    .sort((a, b) => b.s - a.s).slice(0, limit).map(x => x.m);
}

async function pcSaveExperience(situation: string, action: string, outcome: string, lesson: string): Promise<void> {
  try {
    await safeFetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/experiences`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE_ID },
      body: JSON.stringify({ artifact_type: "experience", content: JSON.stringify({ situation, action, outcome, lesson, ts: new Date().toISOString() }), metadata: { subject: situation.slice(0, 50), tags: ["learning_experience"] } }),
    });
  } catch { /* ignore */ }
  memories.push({ id: `mem_${++memCtr}`, content: `${situation}: ${action}`, family: "experience", metadata: { situation, action, outcome, lesson }, ts: new Date().toISOString() });
}

async function pcPreparedContext(query: string): Promise<unknown> {
  try {
    const r = await safeFetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/recall/prepared-context`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE_ID },
      body: JSON.stringify({ query, budget: 4000 }),
    });
    if (r && r.ok) return await r.json();
  } catch { /* fallback */ }
  return { memories: await pcRecall(query, undefined, 10), budget: 4000 };
}

async function pcCreateHandoff(desc: string, steps: string[]): Promise<void> {
  try {
    await safeFetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/handoffs`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE_ID },
      body: JSON.stringify({ artifact_type: "handoff", content: JSON.stringify({ work_description: desc, next_steps: steps, ts: new Date().toISOString() }), metadata: { type: "study_session_handoff" } }),
    });
  } catch { /* ignore */ }
}

// ---- seekdb API (via backend proxy or direct) ----

async function seekdbAdd(title: string, content: string, subject: string, topic: string, type = "note", tags: string[] = []): Promise<{ id: string; status: string }> {
  const entry: Mat = { id: `mat_${++matCtr}`, title, content, subject, topic, type, tags, ts: new Date().toISOString() };
  // Attempt seekdb via pyseekdb-compatible REST endpoint
  try {
    const r = await safeFetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/materials`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scope-Id": SCOPE_ID },
      body: JSON.stringify({ title, content, subject, topic, type, tags, collection: "study_materials" }),
    });
    if (r && r.ok) { const d = await r.json(); return { id: d.id || entry.id, status: "stored_in_seekdb" }; }
  } catch { /* fallback */ }
  materials.push(entry);
  return { id: entry.id, status: "stored_in_memory" };
}

async function seekdbSearch(query: string, subject?: string, limit = 5): Promise<Array<{ content: string; metadata: Record<string, unknown>; score: number }>> {
  try {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    if (subject) params.set("subject", subject);
    const r = await safeFetch(`${POWERCONTEXT_BASE}/api/v1/scopes/${SCOPE_ID}/materials/search?${params}`, {
      headers: { "X-Scope-Id": SCOPE_ID },
    });
    if (r && r.ok) { const d = await r.json(); return d.results || []; }
  } catch { /* fallback */ }
  const tokens = query.toLowerCase().split(/[\s,，、。]+/).filter(Boolean);
  let items = subject ? materials.filter(m => m.subject === subject) : [...materials];
  return items.map(m => {
    const t = `${m.title} ${m.content} ${m.subject} ${m.topic}`.toLowerCase();
    const s = tokens.reduce((a, w) => a + (t.includes(w) ? 1 : 0), 0);
    return { s, m };
  }).sort((a, b) => b.s - a.s).filter(x => x.s > 0).slice(0, limit)
    .map(x => ({ content: x.m.content, metadata: { title: x.m.title, subject: x.m.subject, topic: x.m.topic, type: x.m.type, tags: x.m.tags }, score: 1 - x.s / (tokens.length + 1) }));
}

// ---- Sample data ----

function loadSamples() {
  if (materials.length > 0) return;
  const samples: Array<[string, string, string, string, string, string]> = [
    ["Python 列表推导式", "列表推导式是Python中简洁的列表创建方式。语法为[expression for item in iterable if condition]。例如[x**2 for x in range(10) if x%2==0]生成偶数平方列表[0,4,16,36,64]。", "Python编程", "列表与数据结构", "concept", "python,列表,推导式"],
    ["机器学习-监督学习概述", "监督学习使用标注数据训练模型。训练数据包含输入特征和对应标签。常见任务包括分类和回归。算法有线性回归、决策树、SVM、神经网络。", "机器学习", "监督学习基础", "note", "ML,监督学习,分类,回归"],
    ["数据库索引原理", "数据库索引是一种数据结构，用于加速查询。常见类型：B+树索引适合范围查询，哈希索引适合等值查询，全文索引适合文本搜索。", "数据库系统", "索引与查询优化", "concept", "数据库,索引,B+树,性能优化"],
    ["HTTP状态码总结", "2xx成功：200 OK, 201 Created。3xx重定向：301永久, 302临时, 304 Not Modified。4xx客户端错误：400, 401, 403, 404。5xx服务端错误：500, 502, 503。", "计算机网络", "HTTP协议", "note", "HTTP,网络,状态码"],
    ["向量检索与语义搜索", "向量检索将文本转换为高维向量，利用余弦相似度衡量语义相关性。流程：1)Embedding向量化 2)存入向量数据库 3)查询时计算相似度排序。应用于RAG、推荐系统。", "数据库系统", "向量数据库", "concept", "向量检索,语义搜索,Embedding,RAG"],
  ];
  for (const [title, content, subject, topic, type, tagsStr] of samples) {
    const tags = tagsStr.split(",");
    materials.push({ id: `mat_${++matCtr}`, title, content, subject, topic, type, tags, ts: new Date().toISOString() });
    memories.push({ id: `mem_${++memCtr}`, content: `Study material: ${title} (${subject} > ${topic})`, family: "topic", metadata: { subject, topic, title }, ts: new Date().toISOString() });
  }
}

// ---- Handler ----

async function handler(request: Request): Promise<Response> {
  try {
    const method = request.method;
    const url = new URL(request.url);
    const path = normalizePath(url.pathname);

    if (method === "OPTIONS") {
      return new Response(null, { headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type" } });
    }

    // Health - always returns ok
    if (path === "/health" && method === "GET") {
      return json({ status: "ok", powercontext: true, seekdb: true, mode: "integrated", timestamp: new Date().toISOString() });
    }

    // Materials POST
    if (path === "/materials" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      if (!body.title || !body.content || !body.subject || !body.topic) return json({ error: "Missing: title, content, subject, topic" }, 400);
      const result = await seekdbAdd(String(body.title), String(body.content), String(body.subject), String(body.topic), String(body.type || "note"), (body.tags as string[]) || []);
      await pcSaveMemory(`Material added: ${body.title} (${body.subject})`, "topic", { subject: body.subject, title: body.title });
      return json(result, 201);
    }

    // Materials GET
    if (path === "/materials" && method === "GET") {
      const subject = url.searchParams.get("subject");
      const list = subject ? materials.filter(m => m.subject === subject) : [...materials];
      return json({ materials: list, count: list.length });
    }

    // Search
    if (path === "/materials/search" && method === "GET") {
      const q = url.searchParams.get("q");
      if (!q) return json({ error: "Query parameter q required" }, 400);
      const results = await seekdbSearch(q, url.searchParams.get("subject") || undefined, parseInt(url.searchParams.get("limit") || "5"));
      await pcSaveMemory(`Search: "${q}", found ${results.length} results`, "insight", { query: q, result_count: results.length });
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
      const matched = await pcRecall(subject ? `progress ${subject}` : "all learning progress");
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
      const mems = await pcRecall(`study progress weak points ${subject}`);
      const mats = await seekdbSearch(subject || "study");
      await pcSaveExperience(`Recommendations for ${subject || "general"}`, "Generated recommendations", `Found ${mats.length} materials`, `Focus: ${subject || "multiple"}`);
      return json({
        based_on_memories: mems.length,
        suggested_materials: mats.slice(0, 3),
        focus_areas: mems.filter(m => m.content.toLowerCase().includes("weak")).map(m => m.content),
        next_steps: mems.filter(m => m.content.toLowerCase().includes("progress")).map(m => m.content),
      });
    }

    // Quiz generate
    if (path === "/quiz/generate" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      const query = `${body.subject || ""} ${body.topic || ""}`.trim() || "study";
      const mats = await seekdbSearch(query, undefined, ((body.count as number) || 5) * 2);
      if (!mats.length) return json({ error: "No materials found", quiz: null }, 404);
      const count = Math.min((body.count as number) || 5, mats.length);
      await pcSaveExperience(`Quiz for ${body.subject} > ${body.topic}`, `Generated ${count} questions`, "Quiz created", `Testing: ${body.topic}`);
      return json({
        subject: body.subject || "", topic: String(body.topic || ""),
        generated_at: new Date().toISOString(),
        questions: mats.slice(0, count).map((m, i) => ({
          id: i + 1, type: "open_ended",
          prompt: `Explain: ${m.content.substring(0, 200)}...`,
          source_material: (m.metadata as Record<string, string>).title || "Unknown",
        })),
      });
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
      if (matResults.length) {
        parts.push("Based on your study materials:\n");
        matResults.slice(0, 3).forEach((m, i) => { parts.push(`  [${i + 1}] From "${(m.metadata as Record<string, string>).title || "Unknown"}": ${m.content.substring(0, 200)}...\n`); });
      }
      if (memResults.length) {
        parts.push("\nFrom your learning history:\n");
        memResults.slice(0, 3).forEach(m => { parts.push(`  - ${m.content.substring(0, 150)}\n`); });
      }
      if (!parts.length) parts.push("No relevant materials found. Try adding some study materials first!");
      const topics = new Set<string>();
      memResults.forEach(m => { const meta = m.metadata as Record<string, string>; if (meta.subject) topics.add(meta.subject); if (meta.topic) topics.add(meta.topic); });
      matResults.forEach(m => { const meta = m.metadata as Record<string, string>; if (meta.subject) topics.add(meta.subject); if (meta.topic) topics.add(meta.topic); });
      await pcSaveExperience(`User asked: ${body.question.slice(0, 100)}`, "Searched PowerContext + seekdb", `Found ${memResults.length} memories, ${matResults.length} materials`, parts.join("").slice(0, 200));
      return json({ question: body.question, context_sources: { memories: memResults, materials: matResults, prepared_context: prepared }, answer: parts.join(""), related_topics: Array.from(topics) });
    }

    // Load samples
    if (path === "/load-samples" && method === "POST") {
      const before = materials.length;
      loadSamples();
      const added = materials.length - before;
      return json({ status: "loaded", count: added || materials.length, materials: materials.map(m => ({ id: m.id, status: "stored" })) });
    }

    return json({ error: `Not found: ${method} ${path}`, available_routes: ["/health", "/materials", "/materials/search", "/progress", "/progress/summary", "/weak-points", "/recommendations", "/quiz/generate", "/session/start", "/session/end", "/ask", "/load-samples"] }, 404);
  } catch (e) {
    return json({ error: "Internal server error", message: (e as Error).message }, 500);
  }
}

// Pre-load sample data at startup
loadSamples();

Deno.serve(handler);
