"""资料切块：规格按小节切、代码按符号切、测试按用例切。

切块粒度决定了上下文里「来源」能定位到什么程度。评审要求输出能给出
「适用的规格条目 / 需要修改的代码符号 / 验证用的测试」，所以这里的
symbol 一律落到可点击、可 grep 的粒度：

* 规格：``SPEC-003#3.2``
* 代码：``demo_project/ordercore/refund.py::RefundService.quote_partial_refund``
* 测试：``demo_project/tests/test_refund_shipping_allocation.py::test_full_refund_returns_all_shipping``
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from sddctx.textutil import sha256_text

_DOC_CODE_RE = re.compile(r"^(SPEC-\d+)")
_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)$")
_VERSION_RE = re.compile(r"版本[：:]\s*([vV][\d.]+)")


@dataclass
class Chunk:
    symbol: str
    heading: str
    start_line: int
    end_line: int
    content: str


@dataclass
class Document:
    category: str
    path: str
    title: str
    spec_version: str
    content_sha: str
    chunks: list[Chunk]


def _title_of_markdown(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def chunk_markdown(source: Path, rel_path: str) -> Document:
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()
    doc_code_match = _DOC_CODE_RE.match(source.name)
    doc_code = doc_code_match.group(1) if doc_code_match else source.stem
    version_match = _VERSION_RE.search(text)
    version = version_match.group(1) if version_match else ""

    chunk_starts = [index for index, line in enumerate(lines) if line.startswith("## ")]
    chunks: list[Chunk] = []
    for position, start in enumerate(chunk_starts):
        end = chunk_starts[position + 1] - 1 if position + 1 < len(chunk_starts) else len(lines) - 1
        body = "\n".join(lines[start : end + 1]).strip()
        if len(body) < 20:
            continue
        heading_text = lines[start][3:].strip()
        section_match = _SECTION_RE.match(heading_text)
        if section_match:
            symbol = f"{doc_code}#{section_match.group(1)}"
            heading = section_match.group(2).strip()
        else:
            symbol = f"{doc_code}#{heading_text}"
            heading = heading_text
        chunks.append(
            Chunk(
                symbol=symbol,
                heading=heading,
                start_line=start + 1,
                end_line=end + 1,
                content=body,
            )
        )
    return Document(
        category="spec",
        path=rel_path,
        title=_title_of_markdown(text, source.stem),
        spec_version=version,
        content_sha=sha256_text(text),
        chunks=chunks,
    )


def _slice(lines: list[str], start_line: int, end_line: int) -> str:
    return "\n".join(lines[start_line - 1 : end_line])


def _node_start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", None) or []
    if decorators:
        return min(decorator.lineno for decorator in decorators)
    return node.lineno


def chunk_python(source: Path, rel_path: str, category: str) -> Document:
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return Document(
            category=category,
            path=rel_path,
            title=source.stem,
            spec_version="",
            content_sha=sha256_text(text),
            chunks=[],
        )

    chunks: list[Chunk] = []
    top_level = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    preamble_end = (min(_node_start(node) for node in top_level) - 1) if top_level else len(lines)
    if preamble_end > 0:
        preamble = _slice(lines, 1, preamble_end).strip()
        if len(preamble) > 40:
            chunks.append(
                Chunk(
                    symbol=f"{rel_path}::<module>",
                    heading="module preamble",
                    start_line=1,
                    end_line=preamble_end,
                    content=preamble,
                )
            )

    def push_function(node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str) -> None:
        start = _node_start(node)
        end = node.end_lineno or start
        chunks.append(
            Chunk(
                symbol=f"{rel_path}::{prefix}{node.name}",
                heading=node.name,
                start_line=start,
                end_line=end,
                content=_slice(lines, start, end),
            )
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            push_function(node, "")
        elif isinstance(node, ast.ClassDef):
            start = _node_start(node)
            body = node.body
            if body and isinstance(body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
                header_end = body[0].lineno - 1
            elif body:
                header_end = body[0].end_lineno or start
            else:
                header_end = start
            header = _slice(lines, start, header_end).strip()
            if header:
                chunks.append(
                    Chunk(
                        symbol=f"{rel_path}::{node.name}",
                        heading=node.name,
                        start_line=start,
                        end_line=header_end,
                        content=header,
                    )
                )
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    push_function(item, f"{node.name}.")
    return Document(
        category=category,
        path=rel_path,
        title=source.stem,
        spec_version="",
        content_sha=sha256_text(text),
        chunks=chunks,
    )
