"""共享测试夹具：用领域语言构造订单快照，避免每个用例重复算金额。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ordercore.models import Order, OrderLine
from ordercore.pricing import quote


def build_order(
    order_id: str = "O-1001",
    lines: tuple[OrderLine, ...] | None = None,
    *,
    member_tier: str = "none",
    region: str = "normal",
    coupon_cents: int = 0,
    refunded_cents: int = 0,
) -> Order:
    """按计价规则结算出一张订单快照，返回带最终金额的 Order。"""
    draft = Order(
        order_id=order_id,
        lines=lines
        or (
            OrderLine("SKU-A", 1_500, 1),
            OrderLine("SKU-B", 1_000, 2),
            OrderLine("SKU-C", 1_000, 1),
        ),
        member_tier=member_tier,
        region=region,
        coupon_cents=coupon_cents,
    )
    settled = quote(draft)
    return replace(
        draft,
        discount_cents=settled.discount_cents,
        coupon_cents=settled.coupon_cents,
        shipping_cents=settled.shipping_cents,
        paid_cents=settled.payable_cents,
        refunded_cents=refunded_cents,
    )


@pytest.fixture
def basic_order() -> Order:
    """商品 4500；银卡折扣 135；优惠券 200；运费 800；实付 4965。"""
    return build_order(member_tier="silver", coupon_cents=200)


@pytest.fixture
def remote_order() -> Order:
    """偏远地区订单：商品 4500；银卡折扣 135；优惠券 200；运费 2000；实付 6165。"""
    return build_order(member_tier="silver", coupon_cents=200, region="remote")
