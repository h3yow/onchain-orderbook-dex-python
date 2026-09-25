"""错误分类：所有业务错误都带有可区分的错误码。"""
from __future__ import annotations


class DexError(Exception):
    """所有 DEX 业务错误的基类。"""

    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str = ""):
        super().__init__(message or self.code)
        self.message = message or self.code


class InvalidQuantity(DexError):
    code = "INVALID_QUANTITY"
    http_status = 400


class InvalidPrice(DexError):
    code = "INVALID_PRICE"
    http_status = 400


class UnknownMarket(DexError):
    code = "UNKNOWN_MARKET"
    http_status = 400


class InsufficientBalance(DexError):
    code = "INSUFFICIENT_BALANCE"
    http_status = 400


class InsufficientFrozen(DexError):
    code = "INSUFFICIENT_FROZEN"
    http_status = 400


class OrderNotFound(DexError):
    code = "ORDER_NOT_FOUND"
    http_status = 404


class OrderNotCancellable(DexError):
    code = "ORDER_NOT_CANCELLABLE"
    http_status = 400


class Forbidden(DexError):
    code = "FORBIDDEN"
    http_status = 403


class Unauthorized(DexError):
    code = "UNAUTHORIZED"
    http_status = 401


class MarketPaused(DexError):
    code = "MARKET_PAUSED"
    http_status = 409


class ReferencePriceMissing(DexError):
    code = "REFERENCE_PRICE_MISSING"
    http_status = 409


class ReferencePriceStale(DexError):
    code = "REFERENCE_PRICE_STALE"
    http_status = 409


class PriceDeviationExceeded(DexError):
    code = "PRICE_DEVIATION_EXCEEDED"
    http_status = 400


class IdempotencyConflict(DexError):
    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409
