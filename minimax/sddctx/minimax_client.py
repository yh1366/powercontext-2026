"""MiniMax 文本模型客户端（标准库 urllib，无 SDK 依赖）。

题目要求 MiniMax 参与「代码生成或修改」，因此本模块只做两件事：
把上下文包按对话格式发出去，把原始响应原样落盘。落盘的请求/响应是
``docs/minimax-dev-log.md`` 里那份开发记录的证据来源，评审可以逐条对齐 diff。

接口：``POST {MINIMAX_BASE_URL}/v1/text/chatcompletion_v2``
（MiniMax 开放平台的 chatcompletion_v2 协议，返回 ``choices[0].message.content``）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from sddctx.config import Settings


class MiniMaxError(RuntimeError):
    """调用 MiniMax 失败（缺 Key、网络、限流、业务错误码）。"""


@dataclass
class ChatResult:
    content: str
    model: str
    latency_ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)


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
        raise MiniMaxError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MiniMaxError(f"网络不可达：{exc.reason}") from exc


def chat(
    settings: Settings,
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 8000,
    timeout: float = 300.0,
) -> ChatResult:
    if not settings.minimax_api_key:
        raise MiniMaxError(
            "缺少 MINIMAX_API_KEY：请 export MINIMAX_API_KEY=... 或写入 minimax/.env.local（该文件不入库）"
        )
    model = model or settings.minimax_model
    url = f"{settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    body, latency_ms, headers = _post(url, payload, settings.minimax_api_key, timeout)
    base_resp = body.get("base_resp") or {}
    status_code = base_resp.get("status_code", 0)
    if status_code not in (0, None):
        raise MiniMaxError(f"MiniMax 业务错误 {status_code}: {base_resp.get('status_msg')}")

    choices = body.get("choices") or []
    if not choices:
        raise MiniMaxError(f"响应里没有 choices：{json.dumps(body, ensure_ascii=False)[:400]}")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):  # 兼容多段内容
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content.strip():
        raise MiniMaxError("MiniMax 返回了空内容")

    return ChatResult(
        content=content,
        model=body.get("model", model),
        latency_ms=latency_ms,
        usage=body.get("usage", {}),
        request_id=headers.get("X-Request-Id") or base_resp.get("request_id", ""),
        raw=body,
        request=payload,
    )


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
