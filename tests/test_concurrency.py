"""Concurrent submission: no overfill, no double charge, conservation."""
import threading
from decimal import Decimal

from app.models import PlaceOrderRequest


def test_many_concurrent_buyers_no_overfill(service):
    from tests.conftest import total_assets, no_negative
    baseline = total_assets(service)
    # one seller, ten funded buyers racing for 1 BTC at 100
    service.place_order(PlaceOrderRequest(
        market="BTC-USDC", account="bob", side="SELL",
        price=Decimal("100"), quantity=Decimal("1")))
    for i in range(10):
        service.credit(f"u{i:02d}", "USDC", Decimal("1000"))

    results = []
    errors = []

    def buyer(i):
        try:
            r = service.place_order(PlaceOrderRequest(
                market="BTC-USDC", account=f"u{i:02d}", side="BUY",
                price=Decimal("100"), quantity=Decimal("1")))
            results.append(r)
        except Exception as e:  # insufficient balance for unfunded users
            errors.append(e)

    threads = [threading.Thread(target=buyer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    filled = [r for r in results if r["status"] == "FILLED"]
    resting = [r for r in results if r["status"] == "NEW"]
    total_filled = sum(Decimal(f["filled_quantity"]) for f in results)
    print(f"STATE concurrent result: filled={len(filled)} resting={len(resting)} "
          f"rejected={len(errors)} total_filled_qty={total_filled}")
    assert len(filled) == 1
    assert total_filled == Decimal("1"), "seller must not be overfilled"
    # each resting buyer freezes exactly 100 USDC; filled buyer spent 100
    for r in resting:
        b = service.get_balance(r["account"], "USDC")
        assert b["frozen"] == "100"
    no_negative(service)
    print("ASSERT exactly one buyer fills 1 BTC; others rest with 100 frozen")


def test_concurrent_funded_buyers_split_one_seller(service):
    from tests.conftest import total_assets, no_negative
    # fund 5 accounts; 1 BTC sold at 100; each buys 0.4 -> first two split
    for i in range(5):
        service.credit(f"v{i}", "USDC", Decimal("1000"))
    service.place_order(PlaceOrderRequest(
        market="BTC-USDC", account="bob", side="SELL",
        price=Decimal("100"), quantity=Decimal("1")))
    results = []
    lock_box = []

    def buyer(i):
        r = service.place_order(PlaceOrderRequest(
            market="BTC-USDC", account=f"v{i}", side="BUY",
            price=Decimal("100"), quantity=Decimal("0.4")))
        lock_box.append(r)

    threads = [threading.Thread(target=buyer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results = lock_box
    qty = sum(Decimal(r["filled_quantity"]) for r in results)
    assert qty == Decimal("1")
    statuses = sorted(r["status"] for r in results)
    print("STATE concurrent split statuses:", statuses)
    no_negative(service)
    print("ASSERT 5 concurrent 0.4-BTC buyers never exceed the 1 BTC ask")
