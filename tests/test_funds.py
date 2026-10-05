"""Locked-funds breakdown: per-order reservations and per-asset totals."""
from decimal import Decimal

from app.models import CancelOrderRequest, PlaceOrderRequest


def place(service, account, side, price, qty, key=None, market="BTC-USDC"):
    return service.place_order(PlaceOrderRequest(
        market=market, account=account, side=side,
        price=Decimal(price), quantity=Decimal(qty), idempotency_key=key))


def cancel(service, account, order_id):
    return service.cancel_order(CancelOrderRequest(
        order_id=order_id, account=account))


def asset_row(funds, asset):
    rows = {a["asset"]: a for a in funds["assets"]}
    return rows[asset]


def test_locked_breakdown_lists_each_open_order(service):
    o1 = place(service, "alice", "BUY", "100", "1")     # locks 100 USDC
    o2 = place(service, "alice", "BUY", "90", "2")      # locks 180 USDC
    o3 = place(service, "carol", "SELL", "110", "0.5")  # locks 0.5 BTC
    print("STATE orders:", o1["order_id"], o2["order_id"], o3["order_id"])

    funds = service.get_funds("alice")
    print("STATE alice funds:", funds)
    locked = {row["order_id"]: row for row in funds["locked"]}
    assert set(locked) == {o1["order_id"], o2["order_id"]}
    row = locked[o2["order_id"]]
    assert row["market"] == "BTC-USDC"
    assert row["side"] == "BUY"
    assert row["price"] == "90"
    assert row["remaining_quantity"] == "2"
    assert row["asset"] == "USDC"
    assert Decimal(row["locked_amount"]) == Decimal("180")

    usdc = asset_row(funds, "USDC")
    assert usdc["balance"] == "100000"
    assert Decimal(usdc["frozen"]) == 280
    assert Decimal(usdc["available"]) == Decimal("100000") - 280
    print("ASSERT per-order locked rows and totals agree")


def test_locked_breakdown_filter_by_market(service):
    service.credit("alice", "TST", Decimal("10"))
    a = place(service, "alice", "BUY", "100", "1")
    b = place(service, "alice", "BUY", "100", "1", market="TST-USDC")
    funds = service.get_funds("alice", market="TST-USDC")
    print("STATE filtered locked:", funds["locked"])
    assert [row["order_id"] for row in funds["locked"]] == [b["order_id"]]
    # totals still cover every asset of the account
    assert Decimal(asset_row(funds, "USDC")["frozen"]) == 200
    print("ASSERT market filter narrows rows but totals stay complete")


def test_price_improvement_released_immediately(service):
    """Taker buy crossing a cheaper maker must not keep the excess locked."""
    place(service, "bob", "SELL", "90", "1")
    buy = place(service, "alice", "BUY", "100", "1")  # pays 90, reserved 100
    assert buy["status"] == "FILLED"
    assert buy["fills"][0]["price"] == "90"
    funds = service.get_funds("alice")
    usdc = asset_row(funds, "USDC")
    print("STATE after improved fill: frozen:", usdc["frozen"],
          "available:", usdc["available"])
    assert funds["locked"] == [], "filled order must lock nothing"
    assert Decimal(usdc["frozen"]) == 0
    # paid 90, not 100: the 10 difference is back in available
    assert usdc["available"] == str(Decimal("100000") - Decimal("90"))
    print("ASSERT excess reservation released at settlement")


def test_partial_fill_then_improvement_then_cancel(service):
    place(service, "bob", "SELL", "90", "0.4")
    buy = place(service, "alice", "BUY", "100", "1")  # fills 0.4@90, rests 0.6
    assert buy["status"] == "PARTIALLY_FILLED"
    funds = service.get_funds("alice")
    row = funds["locked"][0]
    print("STATE partial: locked row:", row)
    # only remaining 0.6 * 100 stays locked; improvement on 0.4 released
    assert row["remaining_quantity"] == "0.6"
    assert Decimal(row["locked_amount"]) == Decimal("60")
    assert Decimal(asset_row(funds, "USDC")["frozen"]) == Decimal("60")

    cancel(service, "alice", buy["order_id"])
    funds = service.get_funds("alice")
    print("STATE after cancel: locked:", funds["locked"],
          "assets:", funds["assets"])
    assert funds["locked"] == []
    assert Decimal(asset_row(funds, "USDC")["frozen"]) == 0
    # spent only 0.4*90 = 36
    assert Decimal(asset_row(funds, "USDC")["available"]) == Decimal("100000") - 36
    print("ASSERT cancelled order locks nothing; funds fully accounted")


def test_filled_and_cancelled_orders_lock_nothing(service):
    place(service, "bob", "SELL", "100", "1")
    buy = place(service, "alice", "BUY", "100", "1")
    resting = place(service, "alice", "BUY", "95", "1")
    cancel(service, "alice", resting["order_id"])
    funds = service.get_funds("alice")
    print("STATE locked after fill+cancel:", funds["locked"])
    assert funds["locked"] == []
    assert Decimal(asset_row(funds, "USDC")["frozen"]) == 0
    # no phantom deduction: paid exactly 100 for 1 BTC
    assert Decimal(asset_row(funds, "USDC")["available"]) == Decimal("100000") - 100
    print("ASSERT no order outside the cancellable list holds funds")
