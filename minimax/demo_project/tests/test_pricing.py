"""计价回归测试（SPEC-001）——TASK-0007 不得改变这些行为。"""

from __future__ import annotations

from dataclasses import replace

from ordercore.models import Order, OrderLine
from ordercore.pricing import member_discount, quote

from tests.conftest import build_order


def test_silver_discount_is_three_percent_rounded_half_up() -> None:
    order = build_order(member_tier="silver")
    settled = quote(order)
    assert settled.goods_cents == 4500
    assert settled.discount_cents == 135
    assert settled.shipping_cents == 800
    assert settled.payable_cents == 4500 - 135 + 800


def test_gold_discount_is_eight_percent() -> None:
    assert member_discount(4500, "gold") == 360


def test_coupon_cannot_exceed_amount_after_discount() -> None:
    order = replace(build_order(coupon_cents=0), coupon_cents=100_000)
    settled = quote(order)
    assert settled.coupon_cents == settled.goods_cents - settled.discount_cents


def test_free_shipping_threshold_uses_net_amount() -> None:
    """满 99 元包邮判定的是「扣完折扣和优惠券」之后的金额（SPEC-002#2.3）。"""
    order = Order(
        order_id="O-FREE",
        lines=(OrderLine("SKU-BIG", 9_950, 1),),
        member_tier="none",
        coupon_cents=100,
    )
    settled = quote(order)
    # 商品金额 9950 已经过 9900 的门槛，但扣掉 100 元优惠券后只剩 9850，因此仍收 8 元运费。
    assert settled.goods_cents >= 9_900
    assert settled.shipping_cents == 800
