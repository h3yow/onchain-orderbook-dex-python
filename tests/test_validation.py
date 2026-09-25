"""Input validation: distinguishable error reasons."""
from decimal import Decimal

import pytest

from app.errors import DexError
from app.models import CancelOrderRequest, ErrorCode, PlaceOrderRequest


def place(service, account, side, price, qty, key=None, market="BTC-USDC"):
    return service.place_order(PlaceOrderRequest(
        market=market, account=account, side=side,
        price=Decimal(price), quantity=Decimal(qty), idempotency_key=key))


def assert_code(exc_info, code: ErrorCode):
    assert exc_info.value.code is code
    print(f"ASSERT rejected with {code.value}: {exc_info.value.message}")


def test_invalid_quantity_non_positive(service):
    with pytest.raises(DexError) as ei:
        place(service, "alice", "BUY", "100", "0")
    assert_code(ei, ErrorCode.INVALID_QUANTITY)
    assert "quantity" in ei.value.message


def test_invalid_quantity_lot_size(service):
    with pytest.raises(DexError) as ei:
        place(service, "alice", "BUY", "100", "0.0001")
    assert_code(ei, ErrorCode.INVALID_QUANTITY)


def test_invalid_price_non_positive(service):
    with pytest.raises(DexError) as ei:
        place(service, "alice", "BUY", "-1", "1")
    assert_code(ei, ErrorCode.INVALID_PRICE)


def test_invalid_price_tick_size(service):
    with pytest.raises(DexError) as ei:
        place(service, "alice", "BUY", "100.005", "1")
    assert_code(ei, ErrorCode.INVALID_PRICE)


def test_unknown_market(service):
    with pytest.raises(DexError) as ei:
        place(service, "alice", "BUY", "100", "1", market="ETH-USDC")
    assert_code(ei, ErrorCode.UNKNOWN_MARKET)


def test_insufficient_balance_buy(service):
    with pytest.raises(DexError) as ei:
        place(service, "alice", "BUY", "200", "1000")  # needs 200000 USDC
    assert_code(ei, ErrorCode.INSUFFICIENT_BALANCE)
    assert "available" in ei.value.message


def test_insufficient_balance_sell(service):
    with pytest.raises(DexError) as ei:
        place(service, "alice", "SELL", "100", "1")  # alice holds no BTC
    assert_code(ei, ErrorCode.INSUFFICIENT_BALANCE)


def test_available_balance_excludes_frozen(service):
    place(service, "alice", "BUY", "100", "999")  # freezes 99900 of 100000
    with pytest.raises(DexError) as ei:
        place(service, "alice", "BUY", "100", "2")  # needs 200, only 100 free
    assert_code(ei, ErrorCode.INSUFFICIENT_BALANCE)


def test_insufficient_frozen_is_distinct(service):
    r = place(service, "bob", "SELL", "100", "1")
    service.state.frozen[("bob", "BTC")] = Decimal("-0.5")  # simulate corruption
    try:
        with pytest.raises(DexError) as ei:
            service.cancel_order(CancelOrderRequest(order_id=r["order_id"],
                                                    account="bob"))
        assert_code(ei, ErrorCode.INSUFFICIENT_FROZEN)
    finally:
        service.state.frozen[("bob", "BTC")] = Decimal("0")


def test_cancel_unknown_order(service):
    with pytest.raises(DexError) as ei:
        service.cancel_order(CancelOrderRequest(order_id="ord_nope", account="bob"))
    assert_code(ei, ErrorCode.ORDER_NOT_ACTIVE)


def test_cancel_other_account_rejected(service):
    r = place(service, "bob", "SELL", "100", "1")
    with pytest.raises(DexError) as ei:
        service.cancel_order(CancelOrderRequest(order_id=r["order_id"],
                                                account="alice"))
    assert_code(ei, ErrorCode.ORDER_NOT_ACTIVE)
