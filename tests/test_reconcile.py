"""Reconciliation: conservation + frozen consistency, read-only, restart-safe."""
import copy
from decimal import Decimal

from app.models import CancelOrderRequest, PlaceOrderRequest
from app.service import DexService


def place(service, account, side, price, qty, key=None, market="BTC-USDC"):
    return service.place_order(PlaceOrderRequest(
        market=market, account=account, side=side,
        price=Decimal(price), quantity=Decimal(qty), idempotency_key=key))


def test_reconcile_clean_after_normal_flow(service):
    place(service, "bob", "SELL", "100", "1")
    buy = place(service, "alice", "BUY", "105", "1.5")  # improved price fill
    assert buy["status"] == "PARTIALLY_FILLED"
    resting = place(service, "carol", "SELL", "110", "0.5")
    service.cancel_order(CancelOrderRequest(
        order_id=resting["order_id"], account="carol"))
    rec = service.reconcile()
    print("STATE reconcile after mixed flow:", rec["ok"],
          "accounts:", rec["checked_accounts"])
    assert rec["ok"], rec["discrepancies"]
    assert rec["discrepancies"] == []
    print("ASSERT conservation and frozen consistency hold")


def test_reconcile_is_read_only(service):
    place(service, "alice", "BUY", "100", "1")
    before = copy.deepcopy(service.state)
    rec = service.reconcile()
    assert rec["ok"]
    assert service.state.balances == before.balances
    assert service.state.frozen == before.frozen
    assert service.state.orders.keys() == before.orders.keys()
    assert len(service.state.fills) == len(before.fills)
    print("ASSERT reconcile mutates nothing")


def test_reconcile_works_when_halted_and_without_reference(service):
    # halt the market and wipe the reference: reconcile must still answer
    service.set_halted("BTC-USDC", True)
    service.state.references.pop("BTC-USDC")
    place(service, "alice", "BUY", "100", "1")  # rests, matching blocked
    rec = service.reconcile()
    print("STATE reconcile under halt+no-reference:", rec["ok"])
    assert rec["ok"], rec["discrepancies"]
    print("ASSERT halt and missing reference do not break reconciliation")


def test_reconcile_detects_frozen_mismatch(service):
    place(service, "alice", "BUY", "100", "1")  # locks 100 USDC
    service.state.frozen[("alice", "USDC")] = Decimal("40")  # corrupt
    rec = service.reconcile()
    print("STATE injected mismatch:", rec["discrepancies"])
    assert not rec["ok"]
    d = rec["discrepancies"][0]
    assert d["kind"] == "frozen_mismatch"
    assert d["account"] == "alice"
    assert d["asset"] == "USDC"
    assert d["expected"] == "100"
    assert d["actual"] == "40"
    print("ASSERT mismatch pinpoints account/asset/expected/actual")


def test_reconcile_detects_conservation_break(service):
    service.state.balances[("alice", "USDC")] += Decimal("7")  # phantom credit
    rec = service.reconcile()
    print("STATE injected phantom balance:", rec["discrepancies"])
    assert not rec["ok"]
    kinds = {d["kind"] for d in rec["discrepancies"]}
    assert "asset_conservation" in kinds
    d = next(d for d in rec["discrepancies"]
             if d["kind"] == "asset_conservation")
    assert d["asset"] == "USDC"
    assert Decimal(d["actual"]) == Decimal(d["expected"]) + 7
    print("ASSERT conservation break reported with expected vs actual")


def test_reconcile_detects_negative_balance(service):
    service.state.balances[("bob", "BTC")] = Decimal("-1")
    rec = service.reconcile()
    assert not rec["ok"]
    kinds = {d["kind"] for d in rec["discrepancies"]}
    assert "negative_balance" in kinds
    print("ASSERT negative balance flagged")


def test_reconcile_survives_restart(tmp_path, now_holder):
    path = str(tmp_path / "ledger.json")
    svc = DexService(ledger_path=path, clock=now_holder, admin_token="secret")
    svc.credit("alice", "USDC", Decimal("5000"))
    svc.credit("bob", "BTC", Decimal("3"))
    svc.set_reference_price("BTC-USDC", Decimal("100"))
    place(svc, "bob", "SELL", "100", "1")
    place(svc, "alice", "BUY", "100", "0.4")     # partial fill path
    place(svc, "alice", "BUY", "90", "2")        # resting
    assert svc.reconcile()["ok"]

    # simulate restart: fresh service over the same ledger file
    svc2 = DexService(ledger_path=path, clock=now_holder, admin_token="secret")
    rec = svc2.reconcile()
    print("STATE reconcile after restart:", rec["ok"],
          "accounts:", rec["checked_accounts"])
    assert rec["ok"], rec["discrepancies"]
    # trading still works and stays consistent after the restart
    place(svc2, "alice", "BUY", "100", "0.6")
    assert svc2.reconcile()["ok"]
    print("ASSERT restart preserves reconciliation invariants")
