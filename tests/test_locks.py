"""Funds lockup detail: per-order locks, totals, price-improvement release."""
from decimal import Decimal

from app.models import CancelOrderRequest, PlaceOrderRequest


def place(service, account, side, price, qty, key=None, market="BTC-USDC"):
    return service.place_order(PlaceOrderRequest(
        market=market, account=account, side=side,
        price=Decimal(price), quantity=Decimal(qty), idempotency_key=key))


def cancel(service, account, order_id):
    return service.cancel_order(CancelOrderRequest(
        order_id=order_id, account=account, idempotency_key=None))


def locks_sum(view, asset):
    return sum((Decimal(l["locked"]) for l in view["locks"]
                if l["asset"] == asset), Decimal(0))


def test_locks_list_matches_frozen_exactly(service):
    from tests.conftest import assert_conservation
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    o1 = place(service, "alice", "BUY", "99", "1")      # locks 99 USDC
    o2 = place(service, "alice", "BUY", "101", "0.5")   # locks 50.5 USDC
    o3 = place(service, "bob", "SELL", "105", "2")      # locks 2 BTC
    print("INPUT orders:", o1["order_id"], o2["order_id"], o3["order_id"])

    view = service.get_locks("alice")
    print("STATE alice locks:", view["locks"])
    assert {l["order_id"] for l in view["locks"]} == {o1["order_id"], o2["order_id"]}
    for l in view["locks"]:
        assert l["market"] == "BTC-USDC"
        assert l["side"] == "BUY"
        assert Decimal(l["remaining_quantity"]) > 0
    # list sum == actual frozen, per asset
    assert locks_sum(view, "USDC") == Decimal("149.5")
    assert Decimal(view["totals"]["USDC"]["frozen"]) == Decimal("149.5")
    assert Decimal(view["totals"]["USDC"]["locked"]) == Decimal("149.5")
    assert Decimal(view["totals"]["USDC"]["balance"]) == Decimal("100000")
    assert Decimal(view["totals"]["USDC"]["available"]) == Decimal("100000") - Decimal("149.5")

    # bob's sell locks base asset
    bview = service.get_locks("bob")
    assert locks_sum(bview, "BTC") == Decimal("2")
    assert Decimal(bview["totals"]["BTC"]["frozen"]) == Decimal("2")
    assert_conservation(service, baseline)
    print("ASSERT per-order locks equal account frozen for both assets")


def test_locks_market_filter(service):
    service.credit("alice", "TST", Decimal("10"))
    a = place(service, "alice", "BUY", "100", "1", market="BTC-USDC")
    b = place(service, "alice", "SELL", "100", "1", market="TST-USDC")
    print("INPUT orders in two markets:", a["order_id"], b["order_id"])

    view = service.get_locks("alice", market="BTC-USDC")
    assert [l["order_id"] for l in view["locks"]] == [a["order_id"]]
    assert all(l["market"] == "BTC-USDC" for l in view["locks"])
    # totals stay account-wide even when the list is filtered
    assert Decimal(view["totals"]["TST"]["frozen"]) == Decimal("1")
    assert Decimal(view["totals"]["TST"]["locked"]) == Decimal("0")
    print("ASSERT market filter narrows list, totals remain account-wide")


def test_price_improvement_released_immediately(service):
    from tests.conftest import assert_conservation
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    place(service, "bob", "SELL", "99", "1")
    buy = place(service, "alice", "BUY", "100", "1")  # fills @99, limit 100
    assert buy["status"] == "FILLED"
    print("STATE taker buy filled at 99 against 100 limit")

    view = service.get_locks("alice")
    # order is FILLED: it must lock nothing at all
    assert view["locks"] == []
    assert Decimal(view["totals"]["USDC"]["frozen"]) == Decimal("0")
    # paid 99, not 100: the 1 USDC improvement is back in available
    assert Decimal(view["totals"]["USDC"]["balance"]) == Decimal("100000") - Decimal("99")
    assert Decimal(view["totals"]["USDC"]["available"]) == Decimal("100000") - Decimal("99")
    assert_conservation(service, baseline)
    print("ASSERT filled taker locks nothing; improvement (100-99)*1 returned")


def test_partial_fill_then_cancel_locks_nothing(service):
    from tests.conftest import assert_conservation
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    place(service, "bob", "SELL", "98", "0.5")
    buy = place(service, "alice", "BUY", "100", "1")  # 0.5 filled @98, 0.5 rests
    assert buy["status"] == "PARTIALLY_FILLED"
    view = service.get_locks("alice")
    # remaining 0.5 * limit 100 = 50 locked; improvement on filled part (2) released
    assert locks_sum(view, "USDC") == Decimal("50")
    assert Decimal(view["totals"]["USDC"]["frozen"]) == Decimal("50")
    print("STATE partial fill: frozen =", view["totals"]["USDC"]["frozen"])

    cancel(service, "alice", buy["order_id"])
    view = service.get_locks("alice")
    print("STATE after cancel locks:", view["locks"])
    assert view["locks"] == []
    assert Decimal(view["totals"]["USDC"]["frozen"]) == Decimal("0")
    # spent only 0.5*98 = 49; everything else is available again
    assert Decimal(view["totals"]["USDC"]["available"]) == Decimal("100000") - Decimal("49")
    assert_conservation(service, baseline)
    print("ASSERT cancelled order locks nothing; available restored fully")
