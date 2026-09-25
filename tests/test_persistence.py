"""Restart recovery: orders, balances, fills, idempotency survive."""
from decimal import Decimal

from app.models import (
    CancelOrderRequest,
    PlaceOrderRequest,
    RiskParams,
)
from app.service import DexService


def _make(path, clock):
    return DexService(ledger_path=path, clock=clock, admin_token="secret")


def test_restart_preserves_orders_balances_and_idempotency(tmp_path, now_holder):
    path = str(tmp_path / "ledger.json")
    svc = _make(path, now_holder)
    svc.credit("alice", "USDC", Decimal("10000"))
    svc.credit("bob", "BTC", Decimal("5"))
    svc.set_reference_price("BTC-USDC", Decimal("100"))

    sell = svc.place_order(PlaceOrderRequest(
        market="BTC-USDC", account="bob", side="SELL",
        price=Decimal("100"), quantity=Decimal("2"), idempotency_key="s1"))
    buy = svc.place_order(PlaceOrderRequest(
        market="BTC-USDC", account="alice", side="BUY",
        price=Decimal("100"), quantity=Decimal("1"), idempotency_key="b1"))
    assert buy["status"] == "FILLED"
    before = {
        "alice_btc": svc.get_balance("alice", "BTC")["balance"],
        "bob_usdc": svc.get_balance("bob", "USDC")["balance"],
        "bob_btc_frozen": svc.get_balance("bob", "BTC")["frozen"],
        "sell_remaining": svc.get_order(sell["order_id"])["remaining_quantity"],
        "fills": len(svc.state.fills),
    }
    print("STATE before restart:", before)

    # ---- restart
    svc2 = _make(path, now_holder)
    assert svc2.get_balance("alice", "BTC")["balance"] == before["alice_btc"]
    assert svc2.get_balance("bob", "USDC")["balance"] == before["bob_usdc"]
    assert svc2.get_balance("bob", "BTC")["frozen"] == before["bob_btc_frozen"]
    resting = svc2.get_order(sell["order_id"])
    assert resting["status"] == "PARTIALLY_FILLED"
    assert resting["remaining_quantity"] == before["sell_remaining"]
    assert len(svc2.state.fills) == before["fills"]
    print("ASSERT balances, freeze and resting order restored after restart")

    # resting remainder is matchable again after rebuild
    buy2 = svc2.place_order(PlaceOrderRequest(
        market="BTC-USDC", account="alice", side="BUY",
        price=Decimal("100"), quantity=Decimal("1"), idempotency_key="b2"))
    assert len(buy2["fills"]) == 1 and buy2["fills"][0]["price"] == "100"
    print("ASSERT rebuilt book keeps matching the surviving remainder")

    # idempotency key survived too: same content replays, different conflicts
    replay = svc2.place_order(PlaceOrderRequest(
        market="BTC-USDC", account="alice", side="BUY",
        price=Decimal("100"), quantity=Decimal("1"), idempotency_key="b1"))
    assert replay["order_id"] == buy["order_id"]
    print("ASSERT idempotency results survive restart")


def test_restart_preserves_halt_and_risk(tmp_path, now_holder):
    path = str(tmp_path / "ledger.json")
    svc = _make(path, now_holder)
    svc.set_halted("BTC-USDC", True)
    svc.set_risk_params("BTC-USDC", RiskParams(max_deviation_bps=250,
                                               reference_max_age_ms=5000))
    svc2 = _make(path, now_holder)
    snap = svc2.get_snapshot("BTC-USDC")["markets"]["BTC-USDC"]
    assert snap["halted"] is True
    assert snap["risk"]["max_deviation_bps"] == 250
    assert snap["risk"]["reference_max_age_ms"] == 5000
    print("ASSERT halt flag and risk params persist across restart")


def test_conservation_after_restart(tmp_path, now_holder):
    path = str(tmp_path / "ledger.json")
    svc = _make(path, now_holder)
    svc.credit("alice", "USDC", Decimal("10000"))
    svc.credit("bob", "BTC", Decimal("3"))
    svc.set_reference_price("BTC-USDC", Decimal("100"))
    svc.place_order(PlaceOrderRequest(market="BTC-USDC", account="bob",
                                      side="SELL", price=Decimal("100"),
                                      quantity=Decimal("3")))
    svc.place_order(PlaceOrderRequest(market="BTC-USDC", account="alice",
                                      side="BUY", price=Decimal("100"),
                                      quantity=Decimal("2")))
    svc2 = _make(path, now_holder)
    totals = {}
    for (_, asset), v in svc2.state.balances.items():
        totals[asset] = totals.get(asset, Decimal(0)) + v
    assert totals == {"USDC": Decimal("10000"), "BTC": Decimal("3")}
    print("ASSERT conservation invariant holds after restart:", totals)
