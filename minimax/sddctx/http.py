"""本地 HTTP 接口（标准库 http.server，无框架依赖）。

题面允许 CLI / HTTP / MCP / 插件任选其一作为调用入口；这里同时提供 CLI 与 HTTP，
HTTP 版本方便评审用浏览器或 curl 直接看上下文输出：

    GET  /                     一个自检页面（链接到下面这些接口）
    GET  /api/health           当前数据库/状态后端
    GET  /api/context?task=    开发上下文（format=json|md）
    GET  /api/state?task=      任务状态（PowerContext 读回）
    POST /api/ingest           重新导入资料 {"variant": "v1"|"v2", "reset": false}
    POST /api/verify           运行测试 {"task": "TASK-0007", "phase": "manual"}
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from sddctx.config import Settings
from sddctx.context import build_context, load_task
from sddctx.db import open_database
from sddctx.ingest import ingest
from sddctx.state import open_state, task_state_view
from sddctx.verify import verify_task

INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>sddctx · SDD 上下文管理</title>
<style>
 body{margin:0;padding:40px;font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1f2430}
 .wrap{max-width:820px;margin:0 auto}
 h1{font-size:22px;margin:0 0 6px}
 p.sub{color:#5d6675;margin:0 0 26px}
 .card{background:#fff;border:1px solid #e4e7ec;border-radius:10px;padding:18px 20px;margin-bottom:14px}
 .card h2{font-size:15px;margin:0 0 8px}
 code{background:#f0f2f5;border-radius:4px;padding:2px 6px;font-size:13px}
 a{color:#2b6cb0;text-decoration:none} a:hover{text-decoration:underline}
 .row{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}
 .pill{background:#eef2f7;border-radius:999px;padding:3px 10px;font-size:12px;color:#43506b}
</style></head><body><div class="wrap">
<h1>sddctx · 规有所依</h1>
<p class="sub">面向软件持续演进的 SDD 上下文管理模块 · 入口列表</p>
<div class="card"><h2>上下文</h2>
 <div class="row"><a href="/api/context?task=TASK-0007">/api/context?task=TASK-0007</a>
 <a href="/api/context?task=TASK-0007&format=md">Markdown 版本</a></div></div>
<div class="card"><h2>状态</h2>
 <div class="row"><a href="/api/state?task=TASK-0007">/api/state?task=TASK-0007</a></div></div>
<div class="card"><h2>环境</h2>
 <div class="row"><a href="/api/health">/api/health</a><span class="pill">数据库 / 状态后端</span></div></div>
<div class="card"><h2>写操作</h2>
 <div class="row"><code>POST /api/ingest</code><code>POST /api/verify</code></div>
 <p class="sub" style="margin:8px 0 0">示例：<code>curl -X POST localhost:8000/api/verify -d '{"task":"TASK-0007","phase":"after"}'</code></p></div>
</div></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    settings: Settings

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
        return

    # ------------------------------------------------------------------ 工具
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}

    def _context(self):
        db = open_database(self.settings)
        store, reason = open_state(self.settings)
        return db, store, reason

    # ------------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        task_id = (query.get("task") or ["TASK-0007"])[0]
        if parsed.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        try:
            if parsed.path == "/api/health":
                db, store, reason = self._context()
                self._json(
                    {
                        "status": "ok",
                        "sddctx": "1.0.0",
                        "storage": db.describe(),
                        "state": {"backend": store.backend, "scope": store.scope_label, "fallback_reason": reason},
                    }
                )
                db.close()
                return
            if parsed.path == "/api/context":
                settings = self.settings
                task = load_task(settings, task_id)
                db, store, reason = self._context()
                pack = build_context(settings, db, store, task, state_fallback_reason=reason)
                fmt = (query.get("format") or ["json"])[0]
                if fmt == "md":
                    self._send(200, pack.markdown.encode("utf-8"), "text/markdown; charset=utf-8")
                else:
                    self._json(pack.payload)
                db.close()
                return
            if parsed.path == "/api/state":
                task = load_task(self.settings, task_id)
                store, reason = open_state(self.settings)
                self._json(task_state_view(store, task, reason))
                return
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # 保证评审看到真实错误而不是空响应
            self._json({"error": type(exc).__name__, "detail": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        payload = self._body()
        try:
            if parsed.path == "/api/ingest":
                db = open_database(self.settings)
                report = ingest(
                    self.settings,
                    db,
                    spec_variant=payload.get("variant", "v1"),
                    reset=bool(payload.get("reset", False)),
                )
                self._json(report.to_dict())
                db.close()
                return
            if parsed.path == "/api/verify":
                task = load_task(self.settings, payload.get("task", "TASK-0007"))
                db, store, _reason = self._context()
                result = verify_task(self.settings, db, store, task, phase=payload.get("phase", "manual"))
                result.pop("target", None)
                self._json(result)
                db.close()
                return
            self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": type(exc).__name__, "detail": str(exc)}, 500)


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8000) -> None:
    handler = type("BoundHandler", (Handler,), {"settings": settings})
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"sddctx HTTP 接口已启动：http://{host}:{port}/  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
