"""金额计算工具：整个工程只允许通过这里做取整，避免各处舍入口径不一致。"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("1")


def share(amount_cents: int, numerator: int, denominator: int) -> int:
    """按 ``numerator / denominator`` 摊分 ``amount_cents``，四舍五入到分（HALF_UP）。

    这是 SPEC-003#3.2 与 SPEC-003#3.3 指定的唯一分摊口径：
    先做精确的十进制乘法/除法，再一次性取整，禁止用浮点数相加后再取整。
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator < 0:
        raise ValueError("numerator must not be negative")
    if amount_cents == 0 or numerator == 0:
        return 0
    exact = Decimal(amount_cents) * Decimal(numerator) / Decimal(denominator)
    return int(exact.quantize(_CENT, rounding=ROUND_HALF_UP))


def apply_rate(amount_cents: int, rate: Decimal) -> int:
    """按费率取整（会员折扣用），同样使用 HALF_UP。"""
    if amount_cents == 0:
        return 0
    exact = Decimal(amount_cents) * rate
    return int(exact.quantize(_CENT, rounding=ROUND_HALF_UP))
