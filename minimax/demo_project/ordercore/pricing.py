"""计价规则（SPEC-001）：会员折扣、优惠券与运费的结算顺序。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ordercore import money
from ordercore.models import Order
from ordercore.shipping import calc_shipping

MEMBER_DISCOUNT_RATES: dict[str, Decimal] = {
    "none": Decimal("0"),
    "silver": Decimal("0.03"),
    "gold": Decimal("0.08"),
}


@dataclass(frozen=True)
class Quote:
    """一次结算的完整金额拆解，字段单位为「分」。"""

    goods_cents: int
    discount_cents: int
    coupon_cents: int
    shipping_cents: int
    payable_cents: int


def member_discount(goods_cents: int, member_tier: str) -> int:
    """SPEC-001#1.2：会员折扣按商品金额计算，HALF_UP 取整到分。"""
    try:
        rate = MEMBER_DISCOUNT_RATES[member_tier]
    except KeyError as exc:  # pragma: no cover - 由 Order 校验兜底
        raise ValueError(f"unknown member_tier: {member_tier}") from exc
    return money.apply_rate(goods_cents, rate)


def quote(order: Order) -> Quote:
    """结算顺序：商品金额 → 会员折扣 → 优惠券 → 运费（基于折扣后金额）。

    顺序不可交换：运费门槛看的是「扣掉会员折扣和优惠券之后」的金额（SPEC-002#2.3）。
    """
    goods_cents = order.goods_cents
    discount_cents = member_discount(goods_cents, order.member_tier)
    after_discount = goods_cents - discount_cents
    coupon_cents = min(order.coupon_cents, after_discount)
    net_goods = after_discount - coupon_cents
    shipping_cents = calc_shipping(net_goods, order.region)
    return Quote(
        goods_cents=goods_cents,
        discount_cents=discount_cents,
        coupon_cents=coupon_cents,
        shipping_cents=shipping_cents,
        payable_cents=net_goods + shipping_cents,
    )
