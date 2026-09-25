"""风控测试：暂停期间不得撮合、恢复后可用、参考价缺失/过期/偏离、管理接口鉴权。"""
import logging
import time

import pytest

from dex.errors import (
    Forbidden,
    MarketPaused,
    PriceDeviationExceeded,
    ReferencePriceMissing,
    ReferencePriceStale,
    Unauthorized,
)

logger = logging.getLogger("test.risk")


def test_pause_blocks_matching_and_resume_recovers(engine):
    ask = engine.place_order("bob", "BTC-USD", "SELL", "100", "1", "r1")
    engine.pause("test-admin")
    with pytest.raises(MarketPaused) as exc:
        engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "r2")
    assert exc.value.code == "MARKET_PAUSED"
    logger.info("判定: 暂停期间下单被拒 code=%s", exc.value.code)
    assert engine.list_trades("BTC-USD") == [], "暂停期间不得产生新成交"
    # 撤单不受暂停影响（不新增成交，仅释放冻结）
    cancelled = engine.cancel_order("bob", ask["order_id"])
    assert cancelled["status"] == "CANCELLED"
    # 恢复后撮合可用
    engine.resume("test-admin")
    engine.place_order("bob", "BTC-USD", "SELL", "100", "1", "r3")
    bid = engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "r4")
    assert bid["status"] == "FILLED"
    assert len(engine.list_trades("BTC-USD")) == 1


def test_admin_auth(engine):
    with pytest.raises(Unauthorized):
        engine.pause("")
    with pytest.raises(Forbidden):
        engine.pause("wrong-token")
    with pytest.raises(Forbidden):
        engine.set_risk_params("wrong-token", "BTC-USD", reference_price="100")


def test_reference_price_guards(engine):
    # 缺失
    with pytest.raises(ReferencePriceMissing):
        engine.place_order("alice", "ETH-USD", "BUY", "100", "1", "g1")
    # 偏离阈值（默认 1000bps = 10%）
    with pytest.raises(PriceDeviationExceeded) as exc:
        engine.place_order("alice", "BTC-USD", "BUY", "120", "1", "g2")
    assert exc.value.code == "PRICE_DEVIATION_EXCEEDED"
    logger.info("判定: 价格 120 相对参考价 100 偏离 20%% > 10%% 被拒")
    # 过期
    engine.set_risk_params("test-admin", "BTC-USD", reference_ttl_seconds=1)
    time.sleep(1.1)
    with pytest.raises(ReferencePriceStale):
        engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "g3")
    # 刷新参考价后恢复
    engine.set_risk_params("test-admin", "BTC-USD", reference_price="100")
    ok = engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "g4")
    assert ok["status"] == "OPEN"


def test_risk_param_adjustment(engine):
    engine.set_risk_params("test-admin", "BTC-USD", max_deviation_bps=100)
    with pytest.raises(PriceDeviationExceeded):
        engine.place_order("alice", "BTC-USD", "BUY", "101.5", "1", "a1")
    ok = engine.place_order("alice", "BTC-USD", "BUY", "100.5", "1", "a2")
    assert ok["status"] == "OPEN"
