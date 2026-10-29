"""退款既有行为回归测试（SPEC-003#3.1、#3.3）。

这些断言在 TASK-0007 之前成立，在修改之后也必须继续成立；
它们只断言「商品金额与折扣/优惠券回退」，不涉及本次新增的运费分摊。
"""

from __future__ import annotations

import pytest

from ordercore.models import OrderLine
from ordercore.refund import EmptyRefundError, RefundService, UnknownSkuError

from tests.conftest import build_order


@pytest.fixture
def service() -> RefundService:
    return RefundService()


def test_full_refund_returns_goods_after_discount_and_coupon(service: RefundService, basic_order) -> None:
    """整单退款时商品部分回退：4500 - 135 - 200 = 4165。"""
    quote = service.quote_partial_refund(basic_order, ["SKU-A", "SKU-B", "SKU-C"])
    assert quote.goods_refund_cents == 4165
    assert quote.skus == ("SKU-A", "SKU-B", "SKU-C")


def test_discount_and_coupon_are_prorated_by_goods_share(service: RefundService, basic_order) -> None:
    """退一半商品金额（2000/4500）时，折扣回退 60、优惠券回退 89。"""
    quote = service.quote_partial_refund(basic_order, ["SKU-B"])
    assert quote.goods_refund_cents == 1851


def test_duplicate_skus_are_deduplicated(service: RefundService, basic_order) -> None:
    quote = service.quote_partial_refund(basic_order, ["SKU-A", "SKU-A"])
    assert quote.skus == ("SKU-A",)
    assert quote.goods_refund_cents == 1500 - 45 - 67


def test_unknown_sku_is_rejected(service: RefundService, basic_order) -> None:
    with pytest.raises(UnknownSkuError):
        service.quote_partial_refund(basic_order, ["SKU-NOT-EXIST"])


def test_empty_selection_is_rejected(service: RefundService, basic_order) -> None:
    with pytest.raises(EmptyRefundError):
        service.quote_partial_refund(basic_order, [])


def test_zero_quantity_line_is_rejected_by_model() -> None:
    with pytest.raises(ValueError):
        OrderLine("SKU-X", 100, 0)


def test_order_without_shipping_can_still_be_refunded(service: RefundService) -> None:
    """包邮订单退运费为 0，不能因为除零而崩溃。"""
    order = build_order(
        "O-FREE",
        (OrderLine("SKU-BIG", 12_000, 1),),
        member_tier="none",
    )
    assert order.shipping_cents == 0
    quote = service.quote_partial_refund(order, ["SKU-BIG"])
    assert quote.shipping_refund_cents == 0
    assert quote.total_refund_cents == order.paid_cents
