"""Reference-price gates and trading halt/resume."""
from decimal import Decimal

from app.models import PlaceOrderRequest, RiskParams


def place(service, account, side, price, qty, market="BTC-USDC"):
    return service.place_order(PlaceOrderRequest(
        market=market, account=account, side=side,
        price=Decimal(price), quantity=Decimal(qty)))


def test_halt_blocks_new_fills_and_resume_restores(service):
    # resting sell exists; halt then incoming buy must NOT match
    sell = place(service, "bob", "SELL", "100", "1")
    service.set_halted("BTC-USDC", True)
    buy = place(service, "alice", "BUY", "100", "1")
    print("STATE halted cross: fills=", len(buy["fills"]),
          "blocked=", buy.get("blocked_reason"))
    assert buy["fills"] == []
    assert buy["blocked_reason"] == "MARKET_HALTED"
    assert buy["status"] == "NEW"
    assert service.get_order(sell["order_id"])["status"] == "NEW"
    # resume: new order crosses
    service.set_halted("BTC-USDC", False)
    buy2 = place(service, "alice", "BUY", "100", "1")
    assert len(buy2["fills"]) == 1
    print("ASSERT halt prevents fills; resume allows matching again")


def test_missing_reference_blocks(service, now_holder):
    from app.models import MarketConfig
    service.register_market(MarketConfig(symbol="X-USDC", base_asset="X",
                                         quote_asset="USDC"))
    # no reference set
    service.credit("a", "USDC", Decimal("10000"))
    service.credit("b", "X", Decimal("10"))
    place(service, "b", "SELL", "1", "1", market="X-USDC")
    buy = place(service, "a", "BUY", "1", "1", market="X-USDC")
    assert buy["fills"] == []
    assert buy["blocked_reason"] == "REFERENCE_UNAVAILABLE"
    print("ASSERT missing reference blocks matching but order rests")


def test_stale_reference_blocks(service, now_holder):
    place(service, "bob", "SELL", "100", "1")
    now_holder.advance(60_001)  # default max age 60s
    buy = place(service, "alice", "BUY", "100", "1")
    assert buy["blocked_reason"] == "REFERENCE_STALE" and buy["fills"] == []
    print("ASSERT stale reference (age > 60000ms) blocks matching")
    # fresh reference restores trading
    service.set_reference_price("BTC-USDC", Decimal("100"))
    buy2 = place(service, "alice", "BUY", "100", "1")
    assert len(buy2["fills"]) == 1
    print("ASSERT refreshed reference re-enables matching")


def test_price_out_of_band_blocks(service):
    # default 1000 bps = 10% around ref 100 -> band [90, 110]
    place(service, "bob", "SELL", "115", "1")  # ask outside band
    buy = place(service, "alice", "BUY", "115", "1")
    assert buy["fills"] == []
    print("ASSERT out-of-band ask (115 vs ref 100, 10%) not matched")
    # widening the band allows the trade
    service.set_risk_params("BTC-USDC", RiskParams(max_deviation_bps=2000,
                                                   reference_max_age_ms=60000))
    buy2 = place(service, "alice", "BUY", "115", "1")
    assert len(buy2["fills"]) == 1 and buy2["fills"][0]["price"] == "115"
    print("ASSERT widened band (20%) admits 115")


def test_risk_params_validated(service):
    import pytest
    from app.errors import DexError
    from app.models import ErrorCode
    with pytest.raises(DexError) as ei:
        service.set_risk_params("BTC-USDC", RiskParams(max_deviation_bps=-1,
                                                       reference_max_age_ms=10))
    assert ei.value.code is ErrorCode.INVALID_RISK_PARAMS
    print("ASSERT negative risk params rejected")


def test_halt_does_not_block_cancel(service):
    sell = place(service, "bob", "SELL", "100", "1")
    service.set_halted("BTC-USDC", True)
    from app.models import CancelOrderRequest
    res = service.cancel_order(CancelOrderRequest(order_id=sell["order_id"],
                                                  account="bob"))
    assert res["status"] == "CANCELLED"
    print("ASSERT cancel remains allowed while halted")


def test_order_blocked_by_halt_becomes_matchable_after_resume(service):
    # sell placed during halt, then resume: an incoming buy must match it
    service.set_halted("BTC-USDC", True)
    sell = place(service, "bob", "SELL", "100", "1")
    assert sell["blocked_reason"] == "MARKET_HALTED"
    service.set_halted("BTC-USDC", False)
    buy = place(service, "alice", "BUY", "100", "1")
    assert len(buy["fills"]) == 1
    assert buy["fills"][0]["maker_order_id"] == sell["order_id"]
    print("ASSERT order resting during halt matches immediately after resume")


def test_order_blocked_by_stale_ref_matches_after_refresh(service, now_holder):
    # stale reference: sell rests; refresh reference; incoming buy matches
    now_holder.advance(60_001)
    sell = place(service, "bob", "SELL", "100", "1")
    assert sell["blocked_reason"] == "REFERENCE_STALE"
    service.set_reference_price("BTC-USDC", Decimal("100"))
    buy = place(service, "alice", "BUY", "100", "1")
    assert len(buy["fills"]) == 1
    assert buy["fills"][0]["maker_order_id"] == sell["order_id"]
    print("ASSERT stale-blocked resting order matches after ref refresh")


def test_out_of_band_resting_order_matches_after_band_widened(service):
    # both sides agree at 115 but band initially blocks; widen band, then
    # a new taker must reach the previously blocked resting ask.
    ask = place(service, "bob", "SELL", "115", "1")
    assert ask.get("blocked_reason") is None  # no book to match anyway
    buy = place(service, "alice", "BUY", "115", "1")
    assert buy["fills"] == []
    service.set_risk_params("BTC-USDC", RiskParams(max_deviation_bps=2000,
                                                   reference_max_age_ms=60000))
    buy2 = place(service, "alice", "BUY", "115", "1")
    assert len(buy2["fills"]) == 1
    assert buy2["fills"][0]["maker_order_id"] == ask["order_id"]
    print("ASSERT blocked resting ask matches once risk band widened")
