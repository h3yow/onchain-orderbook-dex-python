"""重启恢复测试：订单、余额、幂等结果在重启后保持一致。"""
import logging
from decimal import Decimal

import pytest

from dex.engine import MatchingEngine
from dex.errors import IdempotencyConflict

logger = logging.getLogger("test.persistence")


def _make_engine(path):
    e = MatchingEngine(data_path=str(path), admin_token="test-admin")
    return e


def test_restart_recovers_state(tmp_path):
    path = tmp_path / "state.json"
    e = _make_engine(path)
    e.deposit("alice", "USD", "100000")
    e.deposit("bob", "BTC", "10")
    e.set_risk_params("test-admin", "BTC-USD", reference_price="100")
    ask = e.place_order("bob", "BTC-USD", "SELL", "100", "2", "p-ask")
    bid = e.place_order("alice", "BTC-USD", "BUY", "101", "1", "p-bid")
    assert bid["status"] == "FILLED"
    totals_before = e._ledger.totals()

    # 模拟重启：同一数据文件新建引擎
    e2 = _make_engine(path)
    logger.info("重启后校验: orders=%d trades=%d",
                len(e2._orders), len(e2._trades))
    # 订单状态一致
    restored_ask = e2.get_order(ask["order_id"])
    assert restored_ask["status"] == "PARTIALLY_FILLED"
    assert restored_ask["remaining"] == "1"
    assert e2.get_order(bid["order_id"])["status"] == "FILLED"
    # 余额一致
    assert e2._ledger.totals() == totals_before
    alice = e2.get_account("alice")["balances"]
    assert alice["BTC"]["available"] == "1"
    # 订单簿恢复：剩余卖单可继续成交
    bid2 = e2.place_order("alice", "BTC-USD", "BUY", "100", "1", "p-bid2")
    assert bid2["status"] == "FILLED"
    assert e2.get_order(ask["order_id"])["status"] == "FILLED"
    e2._ledger.assert_conservation()


def test_restart_preserves_idempotency(tmp_path):
    path = tmp_path / "state.json"
    e = _make_engine(path)
    e.deposit("alice", "USD", "10000")
    e.set_risk_params("test-admin", "BTC-USD", reference_price="100")
    first = e.place_order("alice", "BTC-USD", "BUY", "100", "1", "restart-key")

    e2 = _make_engine(path)
    # 同 key 同内容：复用原结果，不重复冻结
    replay = e2.place_order("alice", "BTC-USD", "BUY", "100", "1", "restart-key")
    logger.info("判定: 重启后幂等重放 order_id=%s", replay["order_id"])
    assert replay["order_id"] == first["order_id"]
    acct = e2.get_account("alice")["balances"]
    assert acct["USD"]["frozen"] == "100", "重启后不得重复扣款"
    # 同 key 不同内容：拒绝
    with pytest.raises(IdempotencyConflict):
        e2.place_order("alice", "BTC-USD", "BUY", "101", "1", "restart-key")


def test_restart_preserves_pause_state(tmp_path):
    path = tmp_path / "state.json"
    e = _make_engine(path)
    e.deposit("alice", "USD", "10000")
    e.set_risk_params("test-admin", "BTC-USD", reference_price="100")
    e.pause("test-admin")

    e2 = _make_engine(path)
    from dex.errors import MarketPaused
    with pytest.raises(MarketPaused):
        e2.place_order("alice", "BTC-USD", "BUY", "100", "1", "pp-1")
    e2.resume("test-admin")
    ok = e2.place_order("alice", "BTC-USD", "BUY", "100", "1", "pp-2")
    assert ok["status"] == "OPEN"
