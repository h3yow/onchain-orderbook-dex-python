"""撮合规则测试：部分成交、价格时间优先、自成交、撤单、参数与余额校验。"""
import logging
from decimal import Decimal

import pytest

from dex.errors import (
    Forbidden,
    InsufficientBalance,
    InvalidPrice,
    InvalidQuantity,
    OrderNotCancellable,
    OrderNotFound,
    UnknownMarket,
)

logger = logging.getLogger("test.matching")


def test_partial_fill_keeps_remainder(engine):
    logger.info("input: maker SELL 2@100, taker BUY 1@101 -> 期望部分成交")
    ask = engine.place_order("bob", "BTC-USD", "SELL", "100", "2", "k1")
    bid = engine.place_order("alice", "BTC-USD", "BUY", "101", "1", "k2")
    assert bid["status"] == "FILLED"
    assert bid["trades"][0]["price"] == "100", "应按 maker 价成交"
    resting = engine.get_order(ask["order_id"])
    logger.info("maker 状态迁移判定: status=%s remaining=%s",
                resting["status"], resting["remaining"])
    assert resting["status"] == "PARTIALLY_FILLED"
    assert resting["remaining"] == "1"
    # 剩余订单仍在簿中，可继续成交
    bid2 = engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "k3")
    assert bid2["status"] == "FILLED"
    assert engine.get_order(ask["order_id"])["status"] == "FILLED"


def test_price_time_priority(engine):
    logger.info("input: 卖单 101(seq先) 与 100(seq后)，买单应优先吃 100")
    engine.place_order("bob", "BTC-USD", "SELL", "101", "1", "p1")
    engine.place_order("bob", "BTC-USD", "SELL", "100", "1", "p2")
    bid = engine.place_order("alice", "BTC-USD", "BUY", "101", "1", "p3")
    assert bid["trades"][0]["price"] == "100", "价格优先：应先成交更优价"
    # 同价位时间优先
    o1 = engine.place_order("bob", "BTC-USD", "SELL", "99", "1", "t1")
    engine.place_order("bob", "BTC-USD", "SELL", "99", "1", "t2")
    bid2 = engine.place_order("alice", "BTC-USD", "BUY", "99", "1", "t3")
    assert bid2["trades"][0]["maker_order_id"] == o1["order_id"], "时间优先：先挂单先成交"


def test_self_trade_skipped(engine):
    logger.info("input: alice 买 99 与卖 98 交叉，同账户不得自成交")
    engine.place_order("alice", "BTC-USD", "BUY", "99", "1", "s1")
    sell = engine.place_order("alice", "BTC-USD", "SELL", "98", "1", "s2")
    assert sell["trades"] == [], "同账户订单必须跳过"
    assert sell["status"] == "OPEN"
    # 第三方可以成交
    buy = engine.place_order("bob", "BTC-USD", "BUY", "98", "1", "s3")
    assert buy["status"] == "FILLED"


def test_cancel_unfreezes_and_terminates(engine):
    order = engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "c1")
    acct = engine.get_account("alice")
    assert acct["balances"]["USD"]["frozen"] == "100"
    cancelled = engine.cancel_order("alice", order["order_id"])
    assert cancelled["status"] == "CANCELLED"
    acct = engine.get_account("alice")
    logger.info("撤单后余额判定: %s", acct["balances"]["USD"])
    assert acct["balances"]["USD"]["frozen"] == "0"
    assert acct["balances"]["USD"]["available"] == "100000"
    with pytest.raises(OrderNotCancellable):
        engine.cancel_order("alice", order["order_id"])
    with pytest.raises(Forbidden):
        engine.place_order("bob", "BTC-USD", "BUY", "100", "1", "c2")
        engine.cancel_order("bob", order["order_id"])
    with pytest.raises(OrderNotFound):
        engine.cancel_order("alice", "no-such-order")


def test_invalid_params_and_balance(engine):
    with pytest.raises(InvalidQuantity):
        engine.place_order("alice", "BTC-USD", "BUY", "100", "0", "v1")
    with pytest.raises(InvalidQuantity):
        engine.place_order("alice", "BTC-USD", "BUY", "100", "-1", "v2")
    with pytest.raises(InvalidPrice):
        engine.place_order("alice", "BTC-USD", "BUY", "0", "1", "v3")
    with pytest.raises(InvalidPrice):
        engine.place_order("alice", "BTC-USD", "BUY", "-5", "1", "v4")
    with pytest.raises(UnknownMarket):
        engine.place_order("alice", "DOGE-USD", "BUY", "1", "1", "v5")
    # 余额不足：冻结 200 * 1000 = 200000 > 100000
    with pytest.raises(InsufficientBalance) as exc:
        engine.place_order("alice", "BTC-USD", "BUY", "105", "1000", "v6")
    assert exc.value.code == "INSUFFICIENT_BALANCE"
    # 失败后余额未被污染
    acct = engine.get_account("alice")
    assert acct["balances"]["USD"]["available"] == "100000"
    engine._ledger.assert_conservation()


def test_settlement_conservation(engine):
    """成交后双方资产总量守恒、无负余额。"""
    totals_before = engine._ledger.totals()
    engine.place_order("bob", "BTC-USD", "SELL", "100", "3", "m1")
    engine.place_order("alice", "BTC-USD", "BUY", "102", "2", "m2")
    engine._ledger.assert_conservation()
    totals_after = engine._ledger.totals()
    logger.info("守恒判定: before=%s after=%s", totals_before, totals_after)
    assert totals_before == totals_after
    alice = engine.get_account("alice")["balances"]
    assert alice["BTC"]["available"] == "12"   # 10 + 2
    assert alice["USD"]["available"] == "99800"  # 100000 - 2*100
    bob = engine.get_account("bob")["balances"]
    assert bob["USD"]["available"] == "100200"
    assert bob["BTC"]["frozen"] == "1"  # 剩余卖单仍冻结
