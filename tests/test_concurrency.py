"""并发测试：多线程并发下单/撤单，不得超额成交、重复扣款、负余额或总量不守恒。"""
import logging
import threading
from decimal import Decimal

logger = logging.getLogger("test.concurrency")


def test_concurrent_takers_no_overfill(engine):
    """1 个 maker 卖 5，20 个并发买单各买 1：总成交不得超过 5。"""
    engine.place_order("bob", "BTC-USD", "SELL", "100", "5", "maker")
    results, errors = [], []

    def buy(i):
        try:
            r = engine.place_order("alice", "BTC-USD", "BUY", "100", "1", f"cc-{i}")
            results.append(r)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=buy, args=(i,)) for i in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, f"并发下不应出现内部错误: {errors}"
    total_filled = sum(Decimal(r["filled_quantity"]) for r in results)
    logger.info("判定: 20 并发买单总成交=%s (maker 仅 5)", total_filled)
    assert total_filled == Decimal("5"), "不得超额成交"
    filled_orders = [r for r in results if r["status"] == "FILLED"]
    assert len(filled_orders) == 5
    engine._ledger.assert_conservation()
    alice = engine.get_account("alice")["balances"]
    assert Decimal(alice["USD"]["available"]) >= 0
    assert Decimal(alice["USD"]["frozen"]) >= 0
    # 成交 5 个：alice 得 5 BTC，花 500 USD；其余 15 单仍冻结
    assert alice["BTC"]["available"] == "15"
    assert alice["USD"]["frozen"] == "1500"


def test_concurrent_order_and_cancel(engine):
    """并发下单与撤单交叉：最终状态必须自洽且守恒。"""
    totals_before = engine._ledger.totals()
    errors = []

    def place(i):
        try:
            side = "BUY" if i % 2 == 0 else "SELL"
            account = "alice" if side == "BUY" else "bob"
            engine.place_order(account, "BTC-USD", side, "100", "0.1", f"pc-{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def cancel_live():
        for oid, o in list(engine._orders.items()):
            if o.is_live:
                try:
                    engine.cancel_order(o.account_id, oid)
                except Exception:  # noqa: BLE001
                    pass  # 并发下订单可能已被成交/取消

    threads = ([threading.Thread(target=place, args=(i,)) for i in range(40)]
               + [threading.Thread(target=cancel_live) for _ in range(4)])
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, f"并发下不应出现内部错误: {errors}"
    engine._ledger.assert_conservation()
    totals_after = engine._ledger.totals()
    logger.info("守恒判定: before=%s after=%s", totals_before, totals_after)
    assert totals_before == totals_after, "资产总量必须守恒"
    # 每个 live 订单的冻结额度与其剩余量一致
    for oid, o in engine._orders.items():
        if o.is_live:
            if o.side.value == "BUY":
                expected = o.price * o.remaining
                assert engine._ledger.frozen(o.account_id, "USD") >= expected
    logger.info("并发测试通过: orders=%d trades=%d",
                len(engine._orders), len(engine._trades))


def test_concurrent_idempotent_submissions(engine):
    """同一幂等键并发提交同内容：只产生一笔订单。"""
    results = []

    def submit():
        results.append(
            engine.place_order("alice", "BTC-USD", "BUY", "100", "1", "same-key"))

    threads = [threading.Thread(target=submit) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    order_ids = {r["order_id"] for r in results}
    logger.info("判定: 10 并发同 key 提交得到订单数=%d", len(order_ids))
    assert len(order_ids) == 1, "同 key 同内容并发必须只生效一次"
    acct = engine.get_account("alice")["balances"]
    assert acct["USD"]["frozen"] == "100", "只应冻结一次资金"
