"""Domain models, enumerations and API schemas (type definitions)."""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, enum.Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class ErrorCode(str, enum.Enum):
    INVALID_QUANTITY = "INVALID_QUANTITY"
    INVALID_PRICE = "INVALID_PRICE"
    UNKNOWN_MARKET = "UNKNOWN_MARKET"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    INSUFFICIENT_FROZEN = "INSUFFICIENT_FROZEN"
    ORDER_NOT_ACTIVE = "ORDER_NOT_ACTIVE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    MARKET_HALTED = "MARKET_HALTED"
    REFERENCE_UNAVAILABLE = "REFERENCE_UNAVAILABLE"
    REFERENCE_STALE = "REFERENCE_STALE"
    PRICE_OUT_OF_BAND = "PRICE_OUT_OF_BAND"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_RISK_PARAMS = "INVALID_RISK_PARAMS"


@dataclass
class Order:
    order_id: str
    account: str
    market: str
    side: Side
    price: Decimal
    quantity: Decimal
    filled_qty: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.NEW
    created_at_ms: int = 0
    sequence: int = 0


@dataclass
class Fill:
    fill_id: str
    market: str
    buy_order_id: str
    sell_order_id: str
    taker_order_id: str
    maker_order_id: str
    buyer: str
    seller: str
    price: Decimal
    quantity: Decimal
    at_ms: int
    sequence: int


@dataclass
class ReferencePrice:
    price: Decimal
    at_ms: int


@dataclass
class RiskParams:
    max_deviation_bps: int = 1000
    reference_max_age_ms: int = 60_000


@dataclass
class MarketConfig:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal = Decimal("0.01")
    lot_size: Decimal = Decimal("0.001")


@dataclass
class LedgerState:
    balances: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    frozen: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    markets: dict[str, MarketConfig] = field(default_factory=dict)
    references: dict[str, ReferencePrice] = field(default_factory=dict)
    risk: dict[str, RiskParams] = field(default_factory=dict)
    halted: dict[str, bool] = field(default_factory=dict)
    idempotency: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    order_sequence: int = 0
    fill_sequence: int = 0


def new_order_id() -> str:
    return "ord_" + uuid.uuid4().hex[:16]


def new_fill_id() -> str:
    return "fil_" + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------- API schemas


class PlaceOrderRequest(BaseModel):
    market: str
    account: str
    side: Side
    price: Decimal
    quantity: Decimal
    idempotency_key: str | None = None


class CancelOrderRequest(BaseModel):
    order_id: str
    account: str
    idempotency_key: str | None = None


class CreditRequest(BaseModel):
    account: str
    asset: str
    amount: Decimal


class ReferencePriceRequest(BaseModel):
    market: str
    price: Decimal


class RiskParamsRequest(BaseModel):
    market: str
    max_deviation_bps: int = Field(default=1000, ge=0)
    reference_max_age_ms: int = Field(default=60_000, ge=0)


class HaltRequest(BaseModel):
    market: str
    halted: bool
