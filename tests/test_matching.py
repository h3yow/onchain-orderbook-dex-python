"""Matching: partial fills, price-time priority, self-trade, cancel."""
from decimal import Decimal

from app.models import CancelOrderRequest, PlaceOrderRequest


def place(service, account, side, price, qty, key=None, market="BTC-USDC"):
    return service.place_order(PlaceOrderRequest(
        market=market, account=account, side=side,
        price=Decimal(price), quantity=Decimal(qty), idempotency_key=key))


def bal(service, account, asset):
    b = service.get_balance(account, asset)
    return Decimal(b["balance"]), Decimal(b["frozen"])


def test_partial_fill_keeps_remaining_order(service, conftest=None):
    from tests.conftest import total_assets, assert_conservation, no_negative
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    sell = place(service, "bob", "SELL", "100", "1")
    assert sell["status"] == "NEW"
    buy = place(service, "alice", "BUY", "100", "0.4")
    print("STATE after cross: buy status:", buy["status"],
          "filled:", buy["filled_quantity"])
    assert buy["status"] == "FILLED"
    assert Decimal(buy["filled_quantity"]) == Decimal("0.4")
    assert len(buy["fills"]) == 1

    resting = service.get_order(sell["order_id"])
    print("STATE resting sell status:", resting["status"],
          "filled:", resting["filled_quantity"],
          "remaining:", resting["remaining_quantity"])
    assert resting["status"] == "PARTIALLY_FILLED"
    assert Decimal(resting["remaining_quantity"]) == Decimal("0.6")

    # book still shows the remainder
    snap = service.get_snapshot("BTC-USDC")["markets"]["BTC-USDC"]
    assert snap["asks"] == [{"price": "100", "quantity": "0.6"}]

    assert_conservation(service, baseline)
    no_negative(service)
    print("ASSERT partial fill conserved assets:", baseline)


def test_price_priority(service):
    from tests.conftest import total_assets, assert_conservation
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    place(service, "bob", "SELL", "102", "1")
    place(service, "carol", "SELL", "99", "1")  # better ask, rests later
    place(service, "bob", "SELL", "101", "1")
    buy = place(service, "alice", "BUY", "102", "1")
    # must hit the 99 offer from carol first
    assert len(buy["fills"]) == 1
    print("STATE price-priority fill price:", buy["fills"][0]["price"],
          "counterparty:", buy["fills"][0]["counterparty"])
    assert buy["fills"][0]["price"] == "99"
    assert buy["fills"][0]["counterparty"] == "carol"
    assert_conservation(service, baseline)
    print("ASSERT best price wins")


def test_time_priority_at_same_price(service, now_holder):
    from tests.conftest import total_assets, assert_conservation
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    first = place(service, "bob", "SELL", "100", "1")
    now_holder.advance(1)
    second = place(service, "carol", "SELL", "100", "1")
    now_holder.advance(1)
    buy = place(service, "alice", "BUY", "100", "1")
    print("STATE same-price match maker:", buy["fills"][0]["maker_order_id"],
          "first:", first["order_id"])
    assert buy["fills"][0]["maker_order_id"] == first["order_id"]
    # the later order still rests untouched
    assert service.get_order(second["order_id"])["status"] == "NEW"
    assert_conservation(service, baseline)
    print("ASSERT FIFO time priority")


def test_multi_level_partial_taker(service):
    from tests.conftest import total_assets, assert_conservation
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    place(service, "bob", "SELL", "99", "0.3")
    place(service, "carol", "SELL", "100", "0.5")
    buy = place(service, "alice", "BUY", "100", "0.9")
    assert buy["status"] == "PARTIALLY_FILLED"
    assert len(buy["fills"]) == 2
    assert buy["fills"][0]["price"] == "99"
    assert buy["fills"][1]["price"] == "100"
    print("STATE taker walked 2 levels, filled:", buy["filled_quantity"])
    # filled 0.3@99 + 0.5@100 = 79.7 cost; froze 90 -> 10.3 quote left frozen
    _, frozen_usdc = bal(service, "alice", "USDC")
    assert frozen_usdc == Decimal("10.3")  # 0.1 qty * 100 + price improvement 0.3
    btc_bal, _ = bal(service, "alice", "BTC")
    assert btc_bal == Decimal("0.8")
    assert_conservation(service, baseline)
    print("ASSERT multi-level fill and leftover freeze correct")


def test_self_trade_prevented(service):
    from tests.conftest import total_assets, assert_conservation, no_negative
    service.credit("carol", "TST", Decimal("10"))
    baseline = total_assets(service)
    sell = place(service, "carol", "SELL", "100", "1", market="TST-USDC")
    buy = place(service, "carol", "BUY", "100", "1", market="TST-USDC")
    print("STATE self-cross: buy fills:", len(buy["fills"]),
          "status:", buy["status"])
    assert buy["fills"] == []
    assert buy["status"] == "NEW"
    # own sell remains valid and resting
    assert service.get_order(sell["order_id"])["status"] == "NEW"
    # both freezes intact
    _, fsell = bal(service, "carol", "TST")
    _, fbuy = bal(service, "carol", "USDC")
    assert fsell == Decimal("1")
    assert fbuy == Decimal("100")
    assert_conservation(service, baseline)
    no_negative(service)
    print("ASSERT same-account orders never match and both stay valid")


def test_cancel_releases_freeze_and_order_inactive(service):
    from tests.conftest import total_assets, assert_conservation, no_negative
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    sell = place(service, "bob", "SELL", "100", "1")
    _, f0 = bal(service, "bob", "BTC")
    assert f0 == Decimal("1")
    res = service.cancel_order(CancelOrderRequest(order_id=sell["order_id"],
                                                  account="bob"))
    assert res["status"] == "CANCELLED"
    _, f1 = bal(service, "bob", "BTC")
    assert f1 == Decimal("0")
    print("STATE cancel: frozen BTC", f0, "->", f1, "status -> CANCELLED")
    with __import__("pytest").raises(Exception):
        service.cancel_order(CancelOrderRequest(order_id=sell["order_id"],
                                                account="bob"))
    assert_conservation(service, baseline)
    no_negative(service)
    print("ASSERT cancel releases freeze and double-cancel rejected")


def test_cancel_partially_filled_releases_remainder(service):
    from tests.conftest import total_assets, assert_conservation
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    sell = place(service, "bob", "SELL", "100", "1")
    place(service, "alice", "BUY", "100", "0.4")
    service.cancel_order(CancelOrderRequest(order_id=sell["order_id"],
                                            account="bob"))
    # bob: 0.4 BTC sold (frozen reduced), 0.6 returned; got 40 USDC
    b, f = bal(service, "bob", "BTC")
    assert b == Decimal("9.6") and f == Decimal("0")
    ub, _ = bal(service, "bob", "USDC")
    assert ub == Decimal("100040")
    assert_conservation(service, baseline)
    print("ASSERT cancel after partial fill releases only the remainder")


def test_no_cross_beyond_limit_price(service):
    from tests.conftest import total_assets, assert_conservation
    service.credit("bob", "TST", Decimal("10"))
    from tests.conftest import total_assets
    baseline = total_assets(service)
    place(service, "bob", "SELL", "105", "1", market="TST-USDC")
    buy = place(service, "alice", "BUY", "100", "1", market="TST-USDC")
    assert buy["fills"] == [] and buy["status"] == "NEW"
    print("STATE buy 100 did not cross ask 105, rests in book")
    assert_conservation(service, baseline)
