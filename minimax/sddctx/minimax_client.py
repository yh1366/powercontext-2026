"""MiniMax 文本模型客户端（标准库 urllib，无 SDK 依赖）。

题目要求 MiniMax 参与「代码生成或修改」，因此本模块只做两件事：
把上下文包按对话格式发出去，把原始响应原样落盘。落盘的请求/响应是
``docs/minimax-dev-log.md`` 里那份开发记录的证据来源，评审可以逐条对齐 diff。

接口：``POST {MINIMAX_BASE_URL}/v1/chat/completions``
（OpenAI 兼容协议；同一套协议在 api.minimaxi.com 与 api.minimax.io 都可用，
因此换区域只改 ``MINIMAX_BASE_URL``）。若服务端不支持该路径，退回到旧版
``/v1/text/chatcompletion_v2``。可用模型用 ``GET {base}/v1/models`` 查询。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from sddctx.config import Settings

_OPENAI_PATH = "/v1/chat/completions"
_LEGACY_PATH = "/v1/text/chatcompletion_v2"
# MiniMax M 系列会在正文前输出思考段（<think>…</think> 或 <mm:think>…</mm:think>），
# 落盘时保留原文，但提取 diff 前要剥掉，避免把思考里的示例代码当成补丁。
_THINK_RE = re.compile(r"<think>.*?</think>|<mm:think>.*?</mm:think>", re.DOTALL)


class MiniMaxError(RuntimeError):
    """调用 MiniMax 失败（缺 Key、网络、限流、业务错误码）。"""


@dataclass
class ChatResult:
    content: str
    model: str
    latency_ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    endpoint: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)


def strip_think(text: str) -> str:
    """去掉思考段，只留最终回答。"""
    return _THINK_RE.sub("", text).strip()


def _post(url: str, payload: dict[str, Any], api_key: str, timeout: float) -> tuple[dict[str, Any], int, dict[str, str]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            headers = {key: value for key, value in response.headers.items()}
            return json.loads(body), int((time.time() - started) * 1000), headers
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise MiniMaxError(f"HTTP {exc.code} @ {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MiniMaxError(f"网络不可达（{url}）：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise MiniMaxError(f"{url} 返回的不是 JSON：{exc}") from exc


def _message_content(body: dict[str, Any]) -> tuple[str, str]:
    """取出 (原文, 去掉思考段后的正文)。"""
    choices = body.get("choices") or []
    if not choices:
        raise MiniMaxError(f"响应里没有 choices：{json.dumps(body, ensure_ascii=False)[:400]}")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):  # 兼容多段内容
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content, strip_think(content)


def chat(
    settings: Settings,
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 8000,
    timeout: float = 600.0,
) -> ChatResult:
    if not settings.minimax_api_key:
        raise MiniMaxError(
            "缺少 MINIMAX_API_KEY：请 export MINIMAX_API_KEY=... 或写入 minimax/.env.local（该文件不入库）"
        )
    model = model or settings.minimax_model
    base = settings.minimax_base_url.rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    errors: list[str] = []
    for path in (_OPENAI_PATH, _LEGACY_PATH):
        url = f"{base}{path}"
        try:
            body, latency_ms, headers = _post(url, payload, settings.minimax_api_key, timeout)
        except MiniMaxError as exc:
            # 只在「路径不存在」时换下一个协议；鉴权/限流/模型名错误直接抛给调用方
            if "HTTP 404" in str(exc) and path == _OPENAI_PATH:
                errors.append(str(exc))
                continue
            raise
        base_resp = body.get("base_resp") or {}
        status_code = base_resp.get("status_code", 0)
        if status_code not in (0, None):
            raise MiniMaxError(f"MiniMax 业务错误 {status_code}: {base_resp.get('status_msg')}")
        raw_content, content = _message_content(body)
        if not content:
            raise MiniMaxError(f"MiniMax 返回了空内容：{json.dumps(body, ensure_ascii=False)[:400]}")
        return ChatResult(
            content=content,
            model=body.get("model", model),
            latency_ms=latency_ms,
            usage=body.get("usage", {}),
            request_id=headers.get("X-Request-Id") or body.get("id") or base_resp.get("request_id", ""),
            endpoint=url,
            raw={**body, "content_raw": raw_content},
            request=payload,
        )

    raise MiniMaxError("；".join(errors) or "没有可用的 MiniMax 接口")


def list_models(settings: Settings, timeout: float = 30.0) -> list[str]:
    """查询当前 Key 在 ``MINIMAX_BASE_URL`` 下可用的模型名。"""
    if not settings.minimax_api_key:
        raise MiniMaxError("缺少 MINIMAX_API_KEY")
    url = f"{settings.minimax_base_url.rstrip('/')}/v1/models"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {settings.minimax_api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MiniMaxError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
    except urllib.error.URLError as exc:
        raise MiniMaxError(f"网络不可达：{exc.reason}") from exc
    return [item.get("id", "") for item in body.get("data", []) if item.get("id")]


def system_prompt() -> str:
    return (
        "你是一名严格遵守规格的 Python 工程师。你会收到一份 SDD 上下文：任务描述、必须遵守的规格条目、"
        "需要修改的代码、必须继续通过的既有测试，以及用于验证的验收测试。\n"
        "要求：\n"
        "1. 只修改生产代码，禁止修改任何测试文件、禁止删除或弱化断言；\n"
        "2. 严格遵守给出的规格条目（含取整口径、边界条件）；\n"
        "3. 不引入新的第三方依赖，不改变已有函数签名，保持既有行为；\n"
        "4. 只输出一个 ```diff 代码块（unified diff，a/ 与 b/ 前缀为 demo_project/ 下的相对路径），"
        "不要输出解释、不要输出完整文件。\n"
        "5. diff 必须能被 `git apply` 直接应用：hunk 头 `@@ -旧起始行,行数 +新起始行,行数 @@` 的"
        "行号以「目标文件完整内容」左侧行号为准；hunk 内的上下文行以单个空格开头、逐字复制，"
        "中间的未改动行不得省略。\n"
    )


def user_prompt(context_prompt: str, extra: str = "") -> str:
    body = [
        "下面是本次开发任务的上下文包（由 seekdb 检索结果组装）：",
        "",
        context_prompt,
        "",
        "请输出最小改动的 unified diff。",
    ]
    if extra:
        body.extend(["", extra])
    return "\n".join(body)
