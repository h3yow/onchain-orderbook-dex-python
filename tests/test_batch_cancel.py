"""Batch cancel: atomicity, idempotency, scope cancel, concurrency."""
import threading
from decimal import Decimal

import pytest

from app.errors import DexError
from app.models import (
    BatchCancelRequest,
    CancelScopeRequest,
    PlaceOrderRequest,
)


def place(service, account, side, price, qty, key=None, market="BTC-USDC"):
    return service.place_order(PlaceOrderRequest(
        market=market, account=account, side=side,
        price=Decimal(price), quantity=Decimal(qty), idempotency_key=key))


def frozen(service, account, asset):
    return Decimal(service.get_balance(account, asset)["frozen"])


def available(service, account, asset):
    return Decimal(service.get_balance(account, asset)["available"])


# ------------------------------------------------------------- atomic batch

def test_batch_cancel_success_releases_all(service):
    orders = [place(service, "alice", "BUY", p, "1")
              for p in ("100", "90", "80")]
    ids = [o["order_id"] for o in orders]
    res = service.cancel_batch(BatchCancelRequest(
        account="alice", order_ids=ids))
    print("STATE batch result:", res["cancelled_count"],
          [r["result"] for r in res["results"]])
    assert res["cancelled_count"] == 3
    assert all(r["result"] == "cancelled" for r in res["results"])
    released = sum(Decimal(r["released_amount"]) for r in res["results"])
    assert released == Decimal("270")
    assert frozen(service, "alice", "USDC") == 0
    for oid in ids:
        assert service.get_order(oid)["status"] == "CANCELLED"
    print("ASSERT all three cancelled, 270 USDC released atomically")


@pytest.mark.parametrize("bad_case", ["missing", "foreign", "finished", "duplicate"])
def test_batch_cancel_invalid_item_aborts_everything(service, bad_case):
    from tests.conftest import total_assets
    baseline = total_assets(service)
    o1 = place(service, "alice", "BUY", "90", "1")
    o2 = place(service, "alice", "BUY", "80", "1")
    good_ids = [o1["order_id"], o2["order_id"]]
    before_frozen = frozen(service, "alice", "USDC")

    if bad_case == "missing":
        ids = good_ids + ["ord_does_not_exist"]
    elif bad_case == "foreign":
        other = place(service, "carol", "BUY", "80", "1")
        ids = good_ids + [other["order_id"]]
    elif bad_case == "finished":
        place(service, "bob", "SELL", "100", "1")
        done = place(service, "alice", "BUY", "100", "1")  # FILLED at once
        assert done["status"] == "FILLED"
        ids = good_ids + [done["order_id"]]
    else:  # duplicate
        ids = good_ids + [o1["order_id"]]

    with pytest.raises(DexError) as exc:
        service.cancel_batch(BatchCancelRequest(account="alice", order_ids=ids))
    print("STATE rejected batch:", exc.value.code.value, "|", exc.value.message)
    assert exc.value.code.value == "INVALID_BATCH"
    assert "item[2]" in exc.value.message  # points at the offending item

    # nothing changed: every order still active, funds untouched
    for oid in good_ids:
        assert service.get_order(oid)["status"] == "NEW"
    assert frozen(service, "alice", "USDC") == before_frozen
    from tests.conftest import assert_conservation
    assert_conservation(service, baseline)
    print(f"ASSERT {bad_case}: whole batch rejected, funds unchanged")


def test_batch_cancel_empty_list_rejected(service):
    with pytest.raises(DexError) as exc:
        service.cancel_batch(BatchCancelRequest(account="alice", order_ids=[]))
    assert exc.value.code.value == "INVALID_BATCH"
    print("ASSERT empty batch rejected without touching funds")


def test_batch_cancel_idempotent_replay(service):
    orders = [place(service, "alice", "BUY", p, "1") for p in ("100", "90")]
    ids = [o["order_id"] for o in orders]
    req = BatchCancelRequest(account="alice", order_ids=ids,
                             idempotency_key="batch-key-1")
    first = service.cancel_batch(req)
    after_first = frozen(service, "alice", "USDC")
    replay = service.cancel_batch(req)
    print("STATE replay equal:", replay == first,
          "frozen after replay:", frozen(service, "alice", "USDC"))
    assert replay == first
    assert frozen(service, "alice", "USDC") == after_first == 0
    # a different payload with the same key conflicts
    with pytest.raises(DexError) as exc:
        service.cancel_batch(BatchCancelRequest(
            account="alice", order_ids=ids[:1],
            idempotency_key="batch-key-1"))
    assert exc.value.code.value == "IDEMPOTENCY_CONFLICT"
    print("ASSERT replay returns original result, no double release")


# ------------------------------------------------------------- scope cancel

def test_cancel_all_skips_inactive_and_reports(service):
    place(service, "bob", "SELL", "100", "1")
    done = place(service, "alice", "BUY", "100", "1")   # FILLED
    active1 = place(service, "alice", "BUY", "90", "1")
    active2 = place(service, "alice", "BUY", "80", "2")
    res = service.cancel_scope(CancelScopeRequest(account="alice"))
    print("STATE scope result: cancelled:", res["cancelled_count"],
          "skipped:", res["skipped_count"])
    by_id = {r["order_id"]: r for r in res["results"]}
    assert by_id[done["order_id"]]["result"] == "skipped"
    assert "FILLED" in by_id[done["order_id"]]["reason"]
    assert by_id[active1["order_id"]]["result"] == "cancelled"
    assert by_id[active2["order_id"]]["result"] == "cancelled"
    assert res["cancelled_count"] == 2
    assert res["skipped_count"] == 1
    assert frozen(service, "alice", "USDC") == 0
    print("ASSERT inactive order skipped with reason, actives cancelled")


def test_cancel_all_market_scope_only(service):
    service.credit("alice", "TST", Decimal("10"))
    a = place(service, "alice", "BUY", "100", "1")
    b = place(service, "alice", "BUY", "100", "1", market="TST-USDC")
    res = service.cancel_scope(CancelScopeRequest(
        account="alice", market="BTC-USDC"))
    print("STATE market-scoped cancel:", res["cancelled_count"])
    assert res["cancelled_count"] == 1
    assert service.get_order(a["order_id"])["status"] == "CANCELLED"
    assert service.get_order(b["order_id"])["status"] == "NEW"
    print("ASSERT only the targeted market was cancelled")


def test_cancel_scope_idempotent_replay(service):
    place(service, "alice", "BUY", "100", "1")
    req = CancelScopeRequest(account="alice", idempotency_key="scope-key")
    first = service.cancel_scope(req)
    replay = service.cancel_scope(req)
    assert replay == first
    assert frozen(service, "alice", "USDC") == 0
    print("ASSERT scope cancel replay returns original result")


def test_cancel_many_orders_in_one_call(service):
    orders = [place(service, "alice", "BUY", str(50 + i % 40), "0.1")
              for i in range(300)]
    res = service.cancel_scope(CancelScopeRequest(account="alice"))
    print("STATE mass cancel:", res["cancelled_count"], "cancelled")
    assert res["cancelled_count"] == 300
    assert frozen(service, "alice", "USDC") == 0
    assert all(service.get_order(o["order_id"])["status"] == "CANCELLED"
               for o in orders)
    print("ASSERT 300 orders cancelled in a single call")


# ------------------------------------------------- interleaved concurrency

def test_batch_cancel_interleaved_with_orders_and_fills(service):
    """Scope cancel racing new orders/fills: conservation must hold."""
    from tests.conftest import total_assets, assert_conservation, no_negative
    baseline = total_assets(service)
    # bob provides liquidity that keeps filling alice's incoming buys
    place(service, "bob", "SELL", "100", "5")
    seeds = [place(service, "alice", "BUY", "95", "0.2") for _ in range(5)]

    stop = threading.Event()
    errors = []

    def placer():
        i = 0
        while not stop.is_set():
            try:
                place(service, "alice", "BUY", "100", "0.1",
                      key=None)
                i += 1
            except DexError as e:
                errors.append(e)

    def canceller():
        while not stop.is_set():
            try:
                service.cancel_scope(CancelScopeRequest(account="alice"))
            except DexError as e:
                errors.append(e)

    threads = ([threading.Thread(target=placer) for _ in range(3)]
               + [threading.Thread(target=canceller) for _ in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join(0.5)
    stop.set()
    for t in threads:
        t.join()

    print("STATE interleave done: errors:", len(errors))
    assert not errors, f"unexpected errors: {errors[:3]}"
    # final sweep: nothing of alice's may remain active
    res = service.cancel_scope(CancelScopeRequest(account="alice"))
    print("STATE final sweep cancelled:", res["cancelled_count"])
    assert service.get_funds("alice")["locked"] == []
    no_negative(service)
    assert_conservation(service, baseline)
    rec = service.reconcile()
    print("STATE reconcile after interleave:", rec["ok"])
    assert rec["ok"], rec["discrepancies"]
    print("ASSERT no negative balances, conservation and reconcile hold")
