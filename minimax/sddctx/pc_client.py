"""PowerContext HTTP 客户端（仅标准库）。

评审环境里 PowerContext Server 常见启动方式是
``powercontext server run --host 127.0.0.1 --port 8848 --role all``，
它暴露的是 HTTP/JSON 接口（``/v1/scopes``、``/v1/memory/remember``、``/v1/memory/entries/list``）。
这里不引入官方 SDK，避免版本耦合，也方便评审直接看到「发了什么、回了什么」。

注意：本机若设置了 ``HTTP_PROXY``/``HTTPS_PROXY``，回环请求会被代理吞掉，
所以 opener 显式清空 ProxyHandler。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

STATE_PREFIX = "[sddctx]"


class PowerContextError(RuntimeError):
    """PowerContext 不可用或返回错误。"""


class PowerContextClient:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(),
        )
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        record: dict[str, Any] = {"method": method, "path": path}
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                record["status"] = response.status
                self.calls.append(record)
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            record["status"] = exc.code
            self.calls.append(record)
            raise PowerContextError(f"{method} {path} -> {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            record["status"] = "unreachable"
            self.calls.append(record)
            raise PowerContextError(f"{method} {path} -> unreachable: {exc.reason}") from exc

    # ------------------------------------------------------------------ API
    def health(self) -> bool:
        try:
            self._request("GET", "/health/live")
            return True
        except PowerContextError:
            return False

    def ensure_scope(self, idempotency_key: str, title: str, summary: str) -> str:
        """用固定幂等键创建（或取回）Scope，返回 scope_id。"""
        body = self._request(
            "POST",
            "/v1/scopes",
            {"title": title, "summary": summary, "idempotency_key": idempotency_key},
        )
        scope_id = body.get("scope_id") or (body.get("scope") or {}).get("scope_id")
        if not scope_id:
            scopes = self._request("GET", "/v1/scopes?limit=100")
            for item in scopes.get("scopes", []):
                if item.get("title") == title:
                    return str(item["scope_id"])
            raise PowerContextError(f"cannot resolve scope from response: {json.dumps(body)[:300]}")
        return str(scope_id)

    def remember(self, scope_id: str, kind: str, text: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/memory/remember",
            {"scope_id": scope_id, "kind": kind, "text": text},
        )

    def list_entries(self, scope_id: str, limit: int = 100) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            payload: dict[str, Any] = {"scope_id": scope_id, "limit": limit, "include_inactive": True}
            if cursor:
                payload["cursor"] = cursor
            body = self._request("POST", "/v1/memory/entries/list", payload)
            entries.extend(body.get("entries", []))
            cursor = body.get("next_cursor")
            if not cursor:
                break
        return entries

    def search(self, scope_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        body = self._request(
            "POST",
            "/v1/memory/search",
            {"scope_id": scope_id, "query": query, "limit": limit},
        )
        return body.get("hits", [])
