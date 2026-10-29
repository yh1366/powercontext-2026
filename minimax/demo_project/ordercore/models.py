"""订单领域模型：订单快照是只读的，金额字段都在下单时结算完毕。"""

from __future__ import annotations

from dataclasses import dataclass, field

MEMBER_TIERS = ("none", "silver", "gold")
REGIONS = ("normal", "remote")


@dataclass(frozen=True)
class OrderLine:
    """订单行，金额一律用「分」的整数表示。"""

    sku: str
    unit_price_cents: int
    quantity: int = 1

    def __post_init__(self) -> None:
        if not self.sku:
            raise ValueError("sku must not be empty")
        if self.unit_price_cents < 0:
            raise ValueError("unit_price_cents must not be negative")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")

    @property
    def amount_cents(self) -> int:
        return self.unit_price_cents * self.quantity


@dataclass(frozen=True)
class Order:
    """已下单的订单快照。

    ``discount_cents`` / ``coupon_cents`` / ``shipping_cents`` / ``paid_cents``
    是下单时由 :func:`ordercore.pricing.quote` 算好后落库的结果，
    ``refunded_cents`` 是累计已退金额（含运费退款），由退款流程回写。
    """

    order_id: str
    lines: tuple[OrderLine, ...]
    member_tier: str = "none"
    region: str = "normal"
    discount_cents: int = 0
    coupon_cents: int = 0
    shipping_cents: int = 0
    paid_cents: int = 0
    refunded_cents: int = 0
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("order_id must not be empty")
        if not self.lines:
            raise ValueError("order must contain at least one line")
        if self.member_tier not in MEMBER_TIERS:
            raise ValueError(f"unknown member_tier: {self.member_tier}")
        if self.region not in REGIONS:
            raise ValueError(f"unknown region: {self.region}")
        for name in ("discount_cents", "coupon_cents", "shipping_cents", "paid_cents", "refunded_cents"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        seen: set[str] = set()
        for line in self.lines:
            if line.sku in seen:
                raise ValueError(f"duplicate sku in order: {line.sku}")
            seen.add(line.sku)

    @property
    def goods_cents(self) -> int:
        """商品金额合计（折扣前）。"""
        return sum(line.amount_cents for line in self.lines)

    def line(self, sku: str) -> OrderLine:
        for line in self.lines:
            if line.sku == sku:
                return line
        raise KeyError(sku)
