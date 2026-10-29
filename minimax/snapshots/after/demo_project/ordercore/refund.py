"""退款规则（SPEC-003）。

本文件是 TASK-0007 的被改对象：现有实现只覆盖了「商品金额 + 折扣/优惠券回退」，
运费分摊（SPEC-003#3.2）与累计退款上限（SPEC-003#3.4）尚未实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

from ordercore import money
from ordercore.models import Order


class RefundError(ValueError):
    """退款入参不合法。"""


class EmptyRefundError(RefundError):
    """没有指定任何要退的 SKU。"""


class UnknownSkuError(RefundError):
    """指定的 SKU 不在订单中。"""


@dataclass(frozen=True)
class RefundQuote:
    """退款试算结果，字段单位为「分」。

    ``warnings`` 用来把「规则降级/被截断」这类需要人工确认的情况显式带给调用方，
    退款单落库时同样会写入审计字段（SPEC-004#4.2）。
    """

    skus: tuple[str, ...]
    goods_refund_cents: int
    shipping_refund_cents: int
    total_refund_cents: int
    warnings: tuple[str, ...] = ()


class RefundService:
    """按订单快照计算退款金额。

    该类不做任何 I/O：退款单落库、审计日志由上层 application service 负责。
    """

    def quote_partial_refund(self, order: Order, skus: Sequence[str]) -> RefundQuote:
        wanted = tuple(dict.fromkeys(skus))
        if not wanted:
            raise EmptyRefundError("at least one sku is required")
        lines = {line.sku: line for line in order.lines}
        unknown = tuple(sku for sku in wanted if sku not in lines)
        if unknown:
            raise UnknownSkuError(f"sku not in order {order.order_id}: {', '.join(unknown)}")

        goods_total = order.goods_cents
        refund_goods = sum(lines[sku].amount_cents for sku in wanted)

        # SPEC-003#3.3：会员折扣与优惠券按商品金额占比回退。
        discount_back = money.share(order.discount_cents, refund_goods, goods_total)
        coupon_back = money.share(order.coupon_cents, refund_goods, goods_total)
        goods_refund = refund_goods - discount_back - coupon_back

        # SPEC-003#3.2：运费按退款商品金额占比分摊，全额退款时直接退还全部运费。
        if refund_goods == goods_total:
            shipping_refund = order.shipping_cents
        else:
            shipping_refund = money.share(order.shipping_cents, refund_goods, goods_total)

        total_refund = goods_refund + shipping_refund
        refundable_balance = order.paid_cents - order.refunded_cents
        warnings: tuple[str, ...] = ()
        if total_refund > refundable_balance:
            total_refund = max(0, refundable_balance)
            goods_refund = min(goods_refund, total_refund)
            shipping_refund = min(shipping_refund, total_refund - goods_refund)
            warnings = ("refund exceeds refundable balance; refund truncated (SPEC-003#3.4)",)
        return RefundQuote(
            skus=wanted,
            goods_refund_cents=goods_refund,
            shipping_refund_cents=shipping_refund,
            total_refund_cents=total_refund,
            warnings=warnings,
        )
