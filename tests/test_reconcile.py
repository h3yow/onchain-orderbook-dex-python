"""Whole-ledger reconciliation: conservation + frozen==locks, edge cases."""
from decimal import Decimal

from app.models import CancelOrderRequest, PlaceOrderRequest
from app.service import DexService


def place(service, account, side, price, qty, key=None, market="BTC-USDC"):
    return service.place_order(PlaceOrderRequest(
        market=market, account=account, side=side,
        price=Decimal(price), quantity=Decimal(qty), idempotency_key=key))


def test_reconcile_clean_after_mixed_activity(service):
    sell = place(service, "bob", "SELL", "99", "1")
    buy = place(service, "alice", "BUY", "100", "0.4")   # fills @99
    resting = place(service, "carol", "SELL", "105", "2")
    print("INPUT fills + resting orders:", sell["order_id"], buy["order_id"],
          resting["order_id"])
    rec = service.reconcile()
    print("STATE reconcile:", rec["ok"], "accounts:", rec["checked_accounts"])
    assert rec["ok"] is True
    assert rec["discrepancies"] == []
    assert rec["checked_accounts"] >= 3
    assert set(rec["checked_assets"]) == {"USDC", "BTC"}

    # cancelling the resting order keeps the ledger consistent
    service.cancel_order(CancelOrderRequest(
        order_id=resting["order_id"], account="carol", idempotency_key=None))
    assert service.reconcile()["ok"] is True
    print("ASSERT reconcile clean after fills, partials and cancels")


def test_reconcile_ignores_halt_and_missing_reference(service):
    # halted market with resting orders
    service.set_halted("BTC-USDC", True)
    place(service, "alice", "BUY", "100", "1")
    # a market with no reference price at all
    from app.models import MarketConfig
    service.register_market(MarketConfig(symbol="NRF-USDC", base_asset="NRF",
                                         quote_asset="USDC"))
    service.credit("carol", "NRF", Decimal("5"))
    place(service, "carol", "SELL", "10", "1", market="NRF-USDC")
    print("INPUT orders resting under halt + missing reference")

    rec = service.reconcile()
    print("STATE reconcile under halt/no-reference:", rec["ok"])
    assert rec["ok"] is True
    service.set_halted("BTC-USDC", False)
    print("ASSERT reconcile usable regardless of halt/reference state")


def test_reconcile_detects_frozen_mismatch(service):
    place(service, "alice", "BUY", "100", "1")  # locks 100 USDC
    # simulate ledger corruption: frozen drift without any order change
    service.state.frozen[("alice", "USDC")] = Decimal("60")
    rec = service.reconcile()
    print("STATE injected corruption, discrepancies:", rec["discrepancies"])
    assert rec["ok"] is False
    mismatch = [d for d in rec["discrepancies"] if d["kind"] == "FROZEN_MISMATCH"]
    assert len(mismatch) == 1
    d = mismatch[0]
    assert d["account"] == "alice" and d["asset"] == "USDC"
    assert d["expected"] == "100" and d["actual"] == "60"
    print("ASSERT mismatch pinpoints account/asset/expected/actual")


def test_reconcile_detects_conservation_break(service):
    place(service, "alice", "BUY", "100", "1")
    # simulate corruption: balance destroyed without a credit/settlement
    service.state.balances[("bob", "USDC")] -= Decimal("7")
    rec = service.reconcile()
    kinds = {d["kind"] for d in rec["discrepancies"]}
    print("STATE conservation break detected:", kinds)
    assert rec["ok"] is False
    assert "ASSET_NOT_CONSERVED" in kinds
    d = next(x for x in rec["discrepancies"]
             if x["kind"] == "ASSET_NOT_CONSERVED")
    assert d["asset"] == "USDC"
    assert Decimal(d["expected"]) == Decimal("300000")
    assert Decimal(d["actual"]) == Decimal("300000") - Decimal("7")
    print("ASSERT conservation discrepancy reports expected vs actual totals")


def test_reconcile_is_read_only(service):
    place(service, "alice", "BUY", "100", "1")
    before = (dict(service.state.balances), dict(service.state.frozen),
              len(service.state.orders))
    service.reconcile()
    after = (dict(service.state.balances), dict(service.state.frozen),
             len(service.state.orders))
    assert before == after
    print("ASSERT reconcile mutates nothing")


def test_reconcile_after_restart(tmp_path, now_holder):
    path = str(tmp_path / "ledger.json")
    svc = DexService(ledger_path=path, clock=now_holder, admin_token="secret")
    svc.credit("alice", "USDC", Decimal("1000"))
    svc.credit("bob", "BTC", Decimal("2"))
    svc.set_reference_price("BTC-USDC", Decimal("100"))
    place(svc, "bob", "SELL", "100", "1")
    buy = place(svc, "alice", "BUY", "100", "0.5")  # partial: 0.5 rests
    assert svc.reconcile()["ok"] is True
    print("STATE pre-restart reconcile ok; restarting service")

    svc2 = DexService(ledger_path=path, clock=now_holder, admin_token="secret")
    rec = svc2.reconcile()
    print("STATE post-restart reconcile:", rec["ok"])
    assert rec["ok"] is True
    # and the resting remainder still locks exactly 0.5 BTC for bob
    locks = svc2.get_locks("bob")
    assert Decimal(locks["totals"]["BTC"]["frozen"]) == Decimal("0.5")
    assert locks["locks"][0]["locked"] == "0.5"
    print("ASSERT reconcile + locks consistent across restart")
