"""TASK-0007 目标功能测试：部分退款运费分摊（SPEC-003#3.2）与累计退款上限（SPEC-003#3.4）。

这组用例在修改前必须全部失败（实现里没有运费分摊、也没有累计上限校验），
修改后必须全部通过；它们由规格直接推导而来，不依赖任何内部实现细节。
"""

from __future__ import annotations

import pytest

from ordercore.models import Order, OrderLine
from ordercore.refund import RefundService

from tests.conftest import build_order


@pytest.fixture
def service() -> RefundService:
    return RefundService()


def test_partial_refund_allocates_shipping_by_goods_share(service: RefundService, basic_order) -> None:
    """SPEC-003#3.2 运费分摊：退 2000/4500 的商品，运费 800 按同一占比分摊 → 800 × 2000 / 4500 = 355.56 → 356。"""
    quote = service.quote_partial_refund(basic_order, ["SKU-B"])
    assert quote.goods_refund_cents == 1851
    assert quote.shipping_refund_cents == 356
    assert quote.total_refund_cents == 2207


def test_full_refund_returns_all_shipping(service: RefundService, basic_order) -> None:
    """SPEC-003#3.2 第 3 条：整单退款必须原路退回全部运费，退款总额等于实付金额。"""
    quote = service.quote_partial_refund(basic_order, ["SKU-A", "SKU-B", "SKU-C"])
    assert quote.shipping_refund_cents == basic_order.shipping_cents == 800
    assert quote.total_refund_cents == basic_order.paid_cents == 4965


def test_remote_region_shipping_is_allocated_too(service: RefundService, remote_order) -> None:
    """SPEC-003#3.2：偏远地区运费 2000 同样按商品金额占比分摊 → 2000 × 1500 / 4500 = 666.67 → 667。"""
    quote = service.quote_partial_refund(remote_order, ["SKU-A"])
    assert quote.goods_refund_cents == 1388
    assert quote.shipping_refund_cents == 667
    assert quote.total_refund_cents == 2055


def test_allocation_rounds_half_up(service: RefundService) -> None:
    """SPEC-003#3.2 取整口径：分摊金额出现 0.5 分时必须 HALF_UP（903 × 500 / 1000 = 451.5 → 452）。"""
    order = Order(
        order_id="O-HALF-UP",
        lines=(OrderLine("SKU-A", 500, 1), OrderLine("SKU-B", 500, 1)),
        shipping_cents=903,
        paid_cents=1_903,
    )
    quote = service.quote_partial_refund(order, ["SKU-A"])
    assert quote.shipping_refund_cents == 452
    assert quote.total_refund_cents == 952


def test_cumulative_refund_is_capped_by_paid_amount(service: RefundService) -> None:
    """SPEC-003#3.4 累计退款上限：同一订单已退 4500 后再退，可退余额 = 实付 4965 - 已退 4500 = 465，超出部分截断并给出告警。"""
    order = build_order(member_tier="silver", coupon_cents=200, refunded_cents=4_500)
    quote = service.quote_partial_refund(order, ["SKU-A", "SKU-B", "SKU-C"])
    assert quote.total_refund_cents == 465
    assert quote.warnings, "被上限截断时必须返回 warning"


def test_cumulative_refund_never_goes_negative(service: RefundService) -> None:
    """SPEC-003#3.4 第 3 条：历史数据已超额退款时，本次可退金额为 0，不得返回负数，并给出告警。"""
    order = build_order(member_tier="silver", coupon_cents=200, refunded_cents=9_999)
    quote = service.quote_partial_refund(order, ["SKU-A"])
    assert quote.total_refund_cents == 0
    assert quote.warnings
