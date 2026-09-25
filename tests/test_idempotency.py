"""Idempotency: same key + same content replays; differing content rejected."""
from decimal import Decimal

import pytest

from app.errors import DexError
from app.models import (
    CancelOrderRequest,
    ErrorCode,
    PlaceOrderRequest,
)


def req(account="alice", side="BUY", price="100", qty="1", market="BTC-USDC",
        key="k"):
    return PlaceOrderRequest(market=market, account=account, side=side,
                             price=Decimal(price), quantity=Decimal(qty),
                             idempotency_key=key)


def test_replay_same_content_returns_original(service):
    r1 = service.place_order(req())
    r2 = service.place_order(req())
    print("ASSERT replay identical: same order_id", r1["order_id"] == r2["order_id"])
    assert r1 == r2
    # no duplicate order was created and balances unchanged
    assert service.get_balance("alice", "USDC")["frozen"] == "100"
    print("ASSERT no double freeze from replay")


def test_replay_after_fill_returns_original_result(service):
    service.place_order(req(account="bob", side="SELL", qty="1", key="mk"))
    r1 = service.place_order(req(qty="1", key="tk"))
    assert r1["status"] == "FILLED"
    r2 = service.place_order(req(qty="1", key="tk"))
    assert r2["order_id"] == r1["order_id"]
    assert r2["fills"] == r1["fills"]
    print("ASSERT filled order replay still returns original fill result")


@pytest.mark.parametrize("mutate", [
    dict(price="101"),
    dict(qty="2"),
    dict(side="SELL"),
    dict(account="bob"),
    dict(market="TST-USDC"),
])
def test_same_key_different_content_rejected(service, mutate):
    service.place_order(req())
    with pytest.raises(DexError) as ei:
        service.place_order(req(**mutate))
    assert ei.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    print(f"ASSERT conflict for mutated field {mutate}: {ei.value.code.value}")
    # original result still replayable and untouched
    again = service.place_order(req())
    assert again["price"] == "100" and again["quantity"] == "1"


def test_request_without_key_is_not_deduped(service):
    r1 = service.place_order(req(key=None))
    r2 = service.place_order(req(key=None))
    assert r1["order_id"] != r2["order_id"]
    print("ASSERT requests without key are independent")


def test_cancel_idempotency(service):
    o = service.place_order(req(account="bob", side="SELL", qty="1", key=None))
    creq = CancelOrderRequest(order_id=o["order_id"], account="bob",
                              idempotency_key="ck")
    c1 = service.cancel_order(creq)
    c2 = service.cancel_order(creq)
    assert c1 == c2 and c1["status"] == "CANCELLED"
    print("ASSERT cancel replay returns original result without error")
    # same key with different order rejected
    with pytest.raises(DexError) as ei:
        service.cancel_order(CancelOrderRequest(
            order_id="ord_other", account="bob", idempotency_key="ck"))
    assert ei.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    print("ASSERT cancel key conflict on differing content")


def test_key_scoped_by_content_not_account(service):
    # alice and bob reusing the same key with different bodies must conflict
    service.place_order(req(account="alice", key="shared"))
    with pytest.raises(DexError) as ei:
        service.place_order(req(account="bob", side="SELL", key="shared"))
    assert ei.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
