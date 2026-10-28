interface Mem { id: string; content: string; family: string; metadata: Record<string, unknown>; ts: string }
interface Mat { id: string; title: string; content: string; subject: string; topic: string; type: string; tags: string[]; ts: string }

const memories: Mem[] = [];
const materials: Mat[] = [];
let memCounter = 0;
let matCounter = 0;

function addMemory(content: string, family: string, metadata: Record<string, unknown> = {}): Mem {
  const entry = { id: `mem_${++memCounter}`, content, family, metadata, ts: new Date().toISOString() };
  memories.push(entry);
  return entry;
}

function recallMemories(query: string, limit = 10): Mem[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const scored = memories.map(m => ({
    score: words.reduce((acc, w) => acc + (m.content.toLowerCase().includes(w) ? 1 : 0), 0),
    item: m,
  }));
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, limit).map(s => s.item);
}

function addMaterial(title: string, content: string, subject: string, topic: string, type = "note", tags: string[] = []): Mat {
  const entry = { id: `mat_${++matCounter}`, title, content, subject, topic, type, tags, ts: new Date().toISOString() };
  materials.push(entry);
  return entry;
}

function searchMaterials(query: string, limit = 5): Array<{ content: string; metadata: Record<string, unknown>; score: number }> {
  const tokens = query.toLowerCase().split(/[\s,，、。]+/).filter(Boolean);
  const scored = materials.map(m => {
    const text = `${m.title} ${m.content} ${m.subject} ${m.topic} ${m.tags.join(" ")}`.toLowerCase();
    const score = tokens.reduce((acc, t) => acc + (text.includes(t) ? 1 : 0), 0);
    return { score, item: m };
  });
  scored.sort((a, b) => b.score - a.score);
  return scored
    .filter(s => s.score > 0)
    .slice(0, limit)
    .map(s => ({
      content: s.item.content,
      metadata: { title: s.item.title, subject: s.item.subject, topic: s.item.topic, type: s.item.type, tags: s.item.tags },
      score: 1 - s.score / (tokens.length + 1),
    }));
}

function loadSampleData(): Array<{ id: string; status: string }> {
  const samples: Array<[string, string, string, string, string, string]> = [
    ["Python 列表推导式", "列表推导式是Python中简洁的列表创建方式。语法为[expression for item in iterable if condition]。例如[x**2 for x in range(10) if x%2==0]生成偶数平方列表[0,4,16,36,64]。列表推导式比for循环更简洁高效。", "Python编程", "列表与数据结构", "concept", "python,列表,推导式"],
    ["机器学习-监督学习概述", "监督学习使用标注数据训练模型。训练数据包含输入特征和对应标签。常见任务包括分类和回归。算法有线性回归、决策树、SVM、神经网络。评估指标有准确率、精确率、召回率、F1分数。", "机器学习", "监督学习基础", "note", "ML,监督学习,分类,回归"],
    ["数据库索引原理", "数据库索引是一种数据结构，用于加速查询。常见类型：B+树索引适合范围查询，哈希索引适合等值查询，全文索引适合文本搜索。索引通过减少磁盘IO提高查询速度，但会增加写入开销和存储空间。", "数据库系统", "索引与查询优化", "concept", "数据库,索引,B+树,性能优化"],
    ["HTTP状态码总结", "2xx成功：200 OK, 201 Created, 204 No Content。3xx重定向：301永久重定向, 302临时重定向, 304 Not Modified。4xx客户端错误：400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found。5xx服务端错误：500 Internal Server Error, 502 Bad Gateway, 503 Service Unavailable。", "计算机网络", "HTTP协议", "note", "HTTP,网络,状态码"],
    ["向量检索与语义搜索", "向量检索将文本转换为高维向量，利用余弦相似度衡量语义相关性。与传统关键词搜索不同，语义搜索能理解同义词和上下文含义。流程：1)Embedding向量化 2)存入向量数据库 3)查询时计算相似度排序。应用于RAG、推荐系统。", "数据库系统", "向量数据库", "concept", "向量检索,语义搜索,Embedding,RAG"],
  ];
  return samples.map(([title, content, subject, topic, type, tagsStr]) => {
    const tags = tagsStr.split(",");
    const mat = addMaterial(title, content, subject, topic, type, tags);
    addMemory(`Sample: ${title} (${subject})`, "topic", { subject, title });
    return { id: mat.id, status: "stored" };
  });
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function normalizePath(rawPath: string): string {
  const match = rawPath.match(/^\/app(?:-[a-z0-9]+)?(\/.*)?$/);
  if (match) {
    return match[1] || "/";
  }
  if (rawPath.startsWith("/functions/v1/app/")) {
    return "/" + rawPath.slice("/functions/v1/app/".length);
  }
  if (rawPath === "/functions/v1/app") return "/";
  return rawPath;
}

async function handler(request: Request): Promise<Response> {
  const method = request.method;
  const url = new URL(request.url);
  const path = normalizePath(url.pathname);

  if (method === "OPTIONS") {
    return new Response(null, {
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      },
    });
  }

  try {
    if (path === "/health" && method === "GET") {
      return json({
        status: "ok",
        powercontext: true,
        seekdb: true,
        mode: "in-memory",
        timestamp: new Date().toISOString(),
      });
    }

    if (path === "/materials" && method === "POST") {
      const body = await request.json() as Record<string, string | string[]>;
      if (!body.title || !body.content || !body.subject || !body.topic) {
        return json({ error: "Missing required fields: title, content, subject, topic" }, 400);
      }
      const result = addMaterial(
        body.title as string, body.content as string, body.subject as string,
        body.topic as string, (body.type as string) || "note", (body.tags as string[]) || []
      );
      addMemory(`Material: ${body.title} (${body.subject})`, "topic", { subject: body.subject, title: body.title });
      return json({ id: result.id, status: "stored" }, 201);
    }

    if (path === "/materials" && method === "GET") {
      const subject = url.searchParams.get("subject");
      const list = subject ? materials.filter(m => m.subject === subject) : materials;
      return json({ materials: list, count: list.length });
    }

    if (path === "/materials/search" && method === "GET") {
      const q = url.searchParams.get("q");
      if (!q) return json({ error: "Query parameter q is required" }, 400);
      const limit = parseInt(url.searchParams.get("limit") || "5");
      const results = searchMaterials(q, limit);
      addMemory(`Search: "${q}", ${results.length} results`, "insight", { query: q, result_count: results.length });
      return json({ query: q, count: results.length, results });
    }

    if (path === "/progress" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      if (!body.subject || !body.topic || body.progress === undefined) {
        return json({ error: "Missing: subject, topic, progress" }, 400);
      }
      const result = addMemory(
        `Progress: ${body.subject} > ${body.topic} = ${body.progress}%. ${body.notes || ""}`,
        "topic",
        { subject: body.subject, topic: body.topic, progress: body.progress }
      );
      return json(result, 201);
    }

    if (path === "/progress/summary" && method === "GET") {
      const subject = url.searchParams.get("subject");
      const query = subject ? `progress ${subject}` : "all progress";
      const matched = recallMemories(query, 50);
      return json({ total_memories: matched.length, memories: matched, subject });
    }

    if (path === "/weak-points" && method === "POST") {
      const body = await request.json() as Record<string, string>;
      if (!body.subject || !body.topic || !body.description) {
        return json({ error: "Missing: subject, topic, description" }, 400);
      }
      const result = addMemory(
        `Weak point: ${body.subject} > ${body.topic} - ${body.description}`,
        "insight",
        { type: "weak_point", subject: body.subject, topic: body.topic }
      );
      return json(result, 201);
    }

    if (path === "/recommendations" && method === "GET") {
      const subject = url.searchParams.get("subject") || "";
      const mems = recallMemories(`study progress ${subject}`, 20);
      const mats = searchMaterials(subject || "study", 5);
      return json({
        based_on_memories: mems.length,
        suggested_materials: mats.slice(0, 3),
        focus_areas: mems.filter(m => m.content.includes("Weak")).map(m => m.content),
        next_steps: mems.filter(m => m.content.includes("Progress")).map(m => m.content),
      });
    }

    if (path === "/quiz/generate" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      const query = `${(body.subject as string) || ""} ${(body.topic as string) || ""}`.trim() || "study";
      const mats = searchMaterials(query, ((body.count as number) || 5) * 2);
      if (!mats.length) return json({ error: "No materials found", quiz: null }, 404);
      const count = Math.min((body.count as number) || 5, mats.length);
      return json({
        subject: body.subject || "",
        topic: body.topic || "",
        generated_at: new Date().toISOString(),
        questions: mats.slice(0, count).map((m, i) => ({
          id: i + 1,
          type: "open_ended",
          prompt: `Explain the key concepts from: ${m.content.substring(0, 200)}...`,
          source_material: (m.metadata as Record<string, string>).title || "Unknown",
        })),
      });
    }

    if (path === "/session/start" && method === "POST") {
      const body = await request.json() as Record<string, string>;
      const context = recallMemories(`study ${body.subject || "General"}`, 10);
      return json({
        session_id: Date.now().toString(),
        subject: body.subject || "General",
        goal: body.goal || "",
        context: { memories: context },
        started_at: new Date().toISOString(),
      });
    }

    if (path === "/session/end" && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      addMemory(`Session ${body.session_id}: ${body.summary}`, "topic", { session_id: body.session_id });
      return json({ status: "saved", session_id: body.session_id });
    }

    if (path === "/ask" && method === "POST") {
      const body = await request.json() as Record<string, string>;
      if (!body.question) return json({ error: "Question required" }, 400);
      const memResults = recallMemories(body.question, 10);
      const matResults = searchMaterials(body.question, 5);
      const parts: string[] = [];
      if (matResults.length) {
        parts.push("Based on your study materials:\n");
        matResults.slice(0, 3).forEach((m, i) => {
          const title = (m.metadata as Record<string, string>).title || "Unknown";
          parts.push(`  [${i + 1}] From "${title}": ${m.content.substring(0, 200)}...\n`);
        });
      }
      if (memResults.length) {
        parts.push("\nFrom your learning history:\n");
        memResults.slice(0, 3).forEach(m => {
          parts.push(`  - ${m.content.substring(0, 150)}\n`);
        });
      }
      if (!parts.length) parts.push("No relevant materials found. Try adding some notes first!");
      const topics = new Set<string>();
      memResults.forEach(m => {
        if (m.metadata.subject) topics.add(m.metadata.subject as string);
        if (m.metadata.topic) topics.add(m.metadata.topic as string);
      });
      matResults.forEach(m => {
        const meta = m.metadata as Record<string, string>;
        if (meta.subject) topics.add(meta.subject);
        if (meta.topic) topics.add(meta.topic);
      });
      return json({
        question: body.question,
        context_sources: { memories: memResults, materials: matResults },
        answer: parts.join(""),
        related_topics: Array.from(topics),
      });
    }

    if (path === "/load-samples" && method === "POST") {
      const results = loadSampleData();
      return json({ status: "loaded", count: results.length, materials: results });
    }

    return json({ error: `Not found: ${method} ${path}` }, 404);
  } catch (e) {
    return json({ error: `Internal error: ${(e as Error).message}` }, 500);
  }
}

Deno.serve(handler);
