"""核心数据模型：订单、成交、市场配置。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, enum.Enum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


@dataclass
class MarketConfig:
    market: str            # 例如 "BTC-USD"
    base_asset: str        # 例如 "BTC"
    quote_asset: str       # 例如 "USD"
    max_deviation_bps: int = 1000      # 允许相对参考价的最大偏离（基点）
    reference_ttl_seconds: int = 300   # 参考价有效期


@dataclass
class Order:
    order_id: str
    account_id: str
    market: str
    side: Side
    price: Decimal
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.OPEN
    seq: int = 0                    # 时间优先序号（单调递增）
    idempotency_key: str = ""

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_live(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)


@dataclass
class Trade:
    trade_id: str
    market: str
    price: Decimal                  # 成交价（maker 价格）
    quantity: Decimal
    maker_order_id: str
    taker_order_id: str
    maker_account_id: str
    taker_account_id: str
    seq: int = 0
