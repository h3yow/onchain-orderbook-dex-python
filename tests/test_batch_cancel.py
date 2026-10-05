"""Batch cancel: atomicity, idempotency, scope mode, concurrency."""
import threading
from decimal import Decimal

from app.models import BatchCancelRequest, PlaceOrderRequest


def place(service, account, side, price, qty, key=None, market="BTC-USDC"):
    return service.place_order(PlaceOrderRequest(
        market=market, account=account, side=side,
        price=Decimal(price), quantity=Decimal(qty), idempotency_key=key))


def batch(service, account, market=None, order_ids=None, key=None):
    return service.batch_cancel(BatchCancelRequest(
        account=account, market=market, order_ids=order_ids,
        idempotency_key=key))


def frozen(service, account, asset):
    return Decimal(service.get_balance(account, asset)["frozen"])


def test_scope_cancel_all_markets(service):
    from tests.conftest import total_assets, assert_conservation
    service.credit("alice", "TST", Decimal("10"))
    baseline = total_assets(service)
    a = place(service, "alice", "BUY", "100", "1")
    b = place(service, "alice", "BUY", "99", "2")
    c = place(service, "alice", "SELL", "100", "1", market="TST-USDC")
    print("INPUT 3 resting orders across 2 markets:", a["order_id"],
          b["order_id"], c["order_id"])

    res = batch(service, "alice", key="scope-all")
    print("STATE batch result:", res["applied"], res["cancelled"],
          res["skipped"], res["failed"])
    assert res["applied"] is True
    assert res["cancelled"] == 3
    assert res["failed"] == 0
    by_id = {r["order_id"]: r for r in res["results"]}
    assert by_id[a["order_id"]]["released"] == "100"
    assert by_id[b["order_id"]]["released"] == "198"
    assert by_id[c["order_id"]]["asset"] == "TST"
    assert frozen(service, "alice", "USDC") == 0
    assert frozen(service, "alice", "TST") == 0
    assert_conservation(service, baseline)
    print("ASSERT all markets cancelled, all reservations released")


def test_scope_cancel_skips_terminated(service):
    sell = place(service, "bob", "SELL", "100", "1")
    buy = place(service, "alice", "BUY", "100", "0.4")  # fills against bob
    resting = place(service, "alice", "BUY", "99", "1")
    print("INPUT alice: filled order", buy["order_id"],
          "resting order", resting["order_id"])

    res = batch(service, "alice", market="BTC-USDC")
    by_id = {r["order_id"]: r for r in res["results"]}
    print("STATE results:", {k: v["status"] for k, v in by_id.items()})
    assert res["applied"] is True
    assert by_id[buy["order_id"]]["status"] == "skipped"
    assert "FILLED" in by_id[buy["order_id"]]["reason"]
    assert by_id[resting["order_id"]]["status"] == "cancelled"
    assert res["cancelled"] == 1 and res["skipped"] == 1
    # bob's resting sell is untouched (different account)
    assert service.get_order(sell["order_id"])["status"] == "PARTIALLY_FILLED"
    print("ASSERT terminated orders skipped with reason, not failures")


def test_explicit_batch_atomic_abort(service):
    from tests.conftest import assert_conservation
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    # a terminated alice order (filled by carol), created first so the
    # fill cannot touch the orders used by the abort cases below
    dead = place(service, "alice", "BUY", "101", "1")
    place(service, "carol", "SELL", "101", "1")
    assert service.get_order(dead["order_id"])["status"] == "FILLED"
    keep1 = place(service, "alice", "BUY", "100", "1")
    keep2 = place(service, "alice", "BUY", "99", "1")
    foreign = place(service, "bob", "SELL", "150", "1")  # rests, no cross
    before = frozen(service, "alice", "USDC")
    assert before == Decimal("199")
    print("INPUT alice resting:", keep1["order_id"], keep2["order_id"],
          "terminated:", dead["order_id"], "foreign:", foreign["order_id"],
          "frozen:", before)

    cases = [
        ("not_found", [keep1["order_id"], "ord_missing"]),
        ("foreign", [keep1["order_id"], foreign["order_id"]]),
        ("duplicate", [keep1["order_id"], keep1["order_id"]]),
        ("terminated", [keep2["order_id"], dead["order_id"]]),
    ]
    for tag, ids in cases:
        res = batch(service, "alice", order_ids=ids, key=f"abort-{tag}")
        print(f"STATE abort[{tag}] applied:", res["applied"],
              "failed:", res["failed"])
        assert res["applied"] is False
        assert res["cancelled"] == 0
        assert res["failed"] >= 1
        bad = [r for r in res["results"] if r["status"] == "failed"]
        assert all(r["reason"] for r in bad)
        ok_items = [r for r in res["results"] if r["status"] != "failed"]
        assert all(r["status"] == "skipped" and r["reason"] == "batch_aborted"
                   for r in ok_items)

    # no aborted batch ever moved funds: both orders still rest, frozen intact
    assert frozen(service, "alice", "USDC") == before
    assert service.get_order(keep1["order_id"])["status"] == "NEW"
    assert service.get_order(keep2["order_id"])["status"] == "NEW"
    assert_conservation(service, baseline)
    print("ASSERT every invalid item aborts the whole batch, funds unmoved")


def test_explicit_batch_success_and_idempotent_replay(service):
    from tests.conftest import assert_conservation
    baseline = {"USDC": Decimal("300000"), "BTC": Decimal("20")}
    a = place(service, "alice", "BUY", "100", "1")
    b = place(service, "alice", "BUY", "99", "2")
    keep = place(service, "alice", "BUY", "98", "1")
    print("INPUT cancel", a["order_id"], b["order_id"], "keep", keep["order_id"])

    res = batch(service, "alice", order_ids=[a["order_id"], b["order_id"]],
                key="exp-1")
    assert res["applied"] is True and res["cancelled"] == 2
    assert frozen(service, "alice", "USDC") == Decimal("98")
    print("STATE after batch: frozen =", frozen(service, "alice", "USDC"))

    replay = batch(service, "alice", order_ids=[a["order_id"], b["order_id"]],
                   key="exp-1")
    assert replay == res  # original result returned verbatim
    assert frozen(service, "alice", "USDC") == Decimal("98")  # no double release
    assert service.get_order(keep["order_id"])["status"] == "NEW"
    assert_conservation(service, baseline)
    print("ASSERT replay returns original result without re-releasing funds")


def test_batch_cancel_idempotency_conflict(service):
    from app.errors import DexError
    a = place(service, "alice", "BUY", "100", "1")
    b = place(service, "alice", "BUY", "99", "1")
    batch(service, "alice", order_ids=[a["order_id"]], key="conflict-1")
    try:
        batch(service, "alice", order_ids=[b["order_id"]], key="conflict-1")
        raise AssertionError("expected IDEMPOTENCY_CONFLICT")
    except DexError as e:
        print("STATE conflict rejected with code:", e.code.value)
        assert e.code.value == "IDEMPOTENCY_CONFLICT"


def test_batch_cancel_interleaved_with_trading(service):
    """Concurrent placements/fills while a big batch cancel runs: the
    service lock serialises everything, so funds stay conserved and no
    balance ever goes negative."""
    from tests.conftest import total_assets, assert_conservation, no_negative
    service.credit("carol", "TST", Decimal("1000"))
    service.credit("alice", "TST", Decimal("1000"))
    baseline = total_assets(service)

    # 200 resting orders for alice in an isolated market
    ids = [place(service, "alice", "BUY", "100", "1", market="TST-USDC")["order_id"]
           for _ in range(200)]
    print("INPUT 200 resting orders, then concurrent cancel/place/trade")

    errors = []

    def do_batch():
        try:
            batch(service, "alice", market="TST-USDC", key="big-batch")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def do_place():
        try:
            for i in range(20):
                place(service, "carol", "SELL", "100", "1", market="TST-USDC")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=do_batch),
               threading.Thread(target=do_place),
               threading.Thread(target=do_place)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    no_negative(service)
    assert_conservation(service, baseline)
    # every alice order is either cancelled or filled; nothing half-released
    rec = service.reconcile()
    print("STATE reconcile after interleaving:", rec["ok"],
          "discrepancies:", len(rec["discrepancies"]))
    assert rec["ok"] is True
    # frozen exactly equals locks of still-active orders
    view = service.get_locks("alice", market="TST-USDC")
    assert Decimal(view["totals"]["USDC"]["frozen"]) == \
        sum((Decimal(l["locked"]) for l in view["locks"]), Decimal(0))
    print("ASSERT conservation + frozen==locks after concurrent batch cancel")
