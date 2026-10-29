"""ordercore —— 订单计价、运费与退款的核心服务（纯标准库实现）。

这个包是一个可运行、带测试的真实业务工程切片，用来作为本作品
「SDD 上下文管理系统」的被改对象。它没有任何第三方依赖，
`pip install -r requirements.txt` 之后可以直接 `pytest`。
"""

from ordercore.money import share
from ordercore.models import Order, OrderLine
from ordercore.pricing import Quote, quote
from ordercore.refund import (
    EmptyRefundError,
    RefundQuote,
    RefundService,
    UnknownSkuError,
)
from ordercore.shipping import calc_shipping

__all__ = [
    "EmptyRefundError",
    "Order",
    "OrderLine",
    "Quote",
    "RefundQuote",
    "RefundService",
    "UnknownSkuError",
    "calc_shipping",
    "quote",
    "share",
]

__version__ = "1.3.0"
