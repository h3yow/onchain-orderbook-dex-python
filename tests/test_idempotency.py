"""幂等测试：同 key 同内容复用结果，同 key 不同内容拒绝。"""
import logging

import pytest

from dex.errors import IdempotencyConflict

logger = logging.getLogger("test.idempotency")


def test_same_key_same_content_replays(engine):
    first = engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "idem-1")
    second = engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "idem-1")
    logger.info("判定: first=%s second=%s", first["order_id"], second["order_id"])
    assert second["order_id"] == first["order_id"], "同 key 同内容必须复用原结果"
    # 只冻结了一次资金
    acct = engine.get_account("alice")
    assert acct["balances"]["USD"]["frozen"] == "100"


def test_same_key_different_content_rejected(engine):
    engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "idem-2")
    with pytest.raises(IdempotencyConflict) as exc:
        engine.place_order("alice", "BTC-USD", "BUY", "101", "1", "idem-2")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(IdempotencyConflict):
        engine.place_order("alice", "BTC-USD", "BUY", "100", "2", "idem-2")
    with pytest.raises(IdempotencyConflict):
        engine.place_order("alice", "BTC-USD", "SELL", "100", "1", "idem-2")
    with pytest.raises(IdempotencyConflict):
        engine.place_order("bob", "BTC-USD", "BUY", "100", "1", "idem-2")


def test_failed_request_not_recorded(engine):
    """失败的请求不占用幂等键，修正后可重试。"""
    from dex.errors import InsufficientBalance
    with pytest.raises(InsufficientBalance):
        engine.place_order("alice", "BTC-USD", "BUY", "100", "99999", "idem-3")
    ok = engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "idem-3")
    assert ok["status"] == "OPEN"
