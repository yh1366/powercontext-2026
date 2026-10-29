"""运费规则（SPEC-002）。"""

from __future__ import annotations

FREE_SHIPPING_THRESHOLD_CENTS = 9_900
REMOTE_FREE_SHIPPING_THRESHOLD_CENTS = 19_900
BASE_FEE_CENTS = 800
REMOTE_SURCHARGE_CENTS = 1_200


def calc_shipping(goods_after_discount_cents: int, region: str = "normal") -> int:
    """按折扣后商品金额计算运费，返回「分」。

    规则（SPEC-002#2.2）：普通地区满 99 元包邮，否则 8 元；偏远地区满 199 元包邮，
    否则 8 元 + 12 元偏远附加费。
    """
    if goods_after_discount_cents < 0:
        raise ValueError("goods_after_discount_cents must not be negative")
    if region == "remote":
        if goods_after_discount_cents >= REMOTE_FREE_SHIPPING_THRESHOLD_CENTS:
            return 0
        return BASE_FEE_CENTS + REMOTE_SURCHARGE_CENTS
    if region == "normal":
        if goods_after_discount_cents >= FREE_SHIPPING_THRESHOLD_CENTS:
            return 0
        return BASE_FEE_CENTS
    raise ValueError(f"unknown region: {region}")
