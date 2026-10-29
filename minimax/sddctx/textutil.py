"""轻量分词与摘要工具。

工程资料是中英混排：规格是中文，代码标识符是英文，两者都要能被同一个检索器命中，
因此这里做「英文按标识符切分（含 snake/camel 拆分）+ 中文按二元组切分」的混合分词。
没有引入 jieba 之类的分词依赖，是为了让评审在干净环境里 ``pip install`` 之后立刻能跑。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
# 规格条目号（SPEC-003#3.2）在规格、代码、测试之间是三方可追溯的锚点，
# 单独作为一个检索词处理，让「任务里引用了哪条规格」直接影响召回。
_REF_RE = re.compile(r"SPEC-\d+#\d+(?:\.\d+)*", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
STOPWORDS = frozenset({"的", "了", "是", "和", "与", "在", "对", "或", "为", "不", "有", "the", "and", "for", "with"})


def _split_identifier(word: str) -> list[str]:
    parts: list[str] = []
    for chunk in word.split("_"):
        if not chunk:
            continue
        for piece in _CAMEL_RE.findall(chunk):
            parts.append(piece.lower())
    return parts


def spec_refs(text: str) -> list[str]:
    """抽取文本里出现的规格条目号（小写），作为强检索词使用。"""
    return [match.group(0).lower() for match in _REF_RE.finditer(text)]


def tokenize(text: str) -> list[str]:
    """把一段文本切成检索词，返回保留重复的有序列表。"""
    tokens: list[str] = spec_refs(text)
    for match in _WORD_RE.finditer(text):
        word = match.group(0)
        lowered = word.lower()
        if len(lowered) >= 2 and lowered not in STOPWORDS:
            tokens.append(lowered)
        if "_" in word or any(ch.isupper() for ch in word):
            for piece in _split_identifier(word):
                if len(piece) >= 2 and piece != lowered and piece not in STOPWORDS:
                    tokens.append(piece)
    for match in _CJK_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            if run not in STOPWORDS:
                tokens.append(run)
            continue
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if bigram not in STOPWORDS:
                tokens.append(bigram)
    return tokens


def term_counts(text: str) -> Counter[str]:
    return Counter(tokenize(text))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_id(*parts: str, length: int = 12) -> str:
    joined = "|".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:length]  # noqa: S324 - 非安全用途


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
