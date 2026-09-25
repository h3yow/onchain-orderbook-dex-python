"""撮合引擎：编排账本、订单簿、幂等与风控。

并发与原子性：所有状态变更都在同一把锁内完成，且每笔成交先校验后修改，
因此并发提交下不会出现超额成交、重复扣款、负余额或总量不守恒。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation

from .errors import (
    Forbidden,
    InsufficientBalance,
    InvalidPrice,
    InvalidQuantity,
    OrderNotCancellable,
    OrderNotFound,
    Unauthorized,
    UnknownMarket,
)
from .idempotency import IdempotencyStore, fingerprint
from .ledger import Ledger
from .models import MarketConfig, Order, OrderStatus, Side, Trade
from .orderbook import OrderBook
from .persistence import JsonStore
from .risk import RiskController

logger = logging.getLogger("dex.engine")

ZERO = Decimal("0")

DEFAULT_MARKETS = {
    "BTC-USD": MarketConfig("BTC-USD", "BTC", "USD"),
    "ETH-USD": MarketConfig("ETH-USD", "ETH", "USD"),
}


class MatchingEngine:
    def __init__(self, data_path: str | None = None,
                 admin_token: str | None = None,
                 markets: dict[str, MarketConfig] | None = None) -> None:
        self._lock = threading.RLock()
        self._markets = markets or dict(DEFAULT_MARKETS)
        self._ledger = Ledger()
        self._books = {m: OrderBook(m) for m in self._markets}
        self._orders: dict[str, Order] = {}      # 全部订单（含已终结）
        self._trades: list[Trade] = []
        self._idempotency = IdempotencyStore()
        self._risk = RiskController(self._markets)
        self._order_seq = 0
        self._trade_seq = 0
        self._admin_token = admin_token or os.environ.get("DEX_ADMIN_TOKEN", "admin-secret")
        self._store = JsonStore(data_path) if data_path else None
        if self._store:
            self._restore(self._store.load())

    # ---------- 持久化 ----------

    def _snapshot(self) -> dict:
        return {
            "ledger": self._ledger.snapshot(),
            "orders": {oid: self._order_to_state(o) for oid, o in self._orders.items()},
            "trades": [self._trade_view(t) for t in self._trades],
            "idempotency": self._idempotency.snapshot(),
            "risk": self._risk.snapshot(),
            "order_seq": self._order_seq,
            "trade_seq": self._trade_seq,
        }

    def _persist(self) -> None:
        if self._store:
            self._store.save(self._snapshot())

    def _restore(self, data: dict) -> None:
        if not data:
            return
        self._ledger.restore(data.get("ledger", {}))
        self._idempotency.restore(data.get("idempotency", {}))
        self._risk.restore(data.get("risk", {}))
        self._order_seq = data.get("order_seq", 0)
        self._trade_seq = data.get("trade_seq", 0)
        for oid, st in data.get("orders", {}).items():
            order = self._order_from_state(st)
            self._orders[oid] = order
            if order.is_live:
                self._books[order.market].add(order)
        self._trades = [
            Trade(t["trade_id"], t["market"], Decimal(t["price"]), Decimal(t["quantity"]),
                  t["maker_order_id"], t["taker_order_id"],
                  t["maker_account_id"], t["taker_account_id"], t["seq"])
            for t in data.get("trades", [])
        ]
        self._ledger.assert_conservation()
        logger.info("restored: orders=%d trades=%d", len(self._orders), len(self._trades))

    @staticmethod
    def _order_to_state(o: Order) -> dict:
        return {
            "order_id": o.order_id, "account_id": o.account_id, "market": o.market,
            "side": o.side.value, "price": str(o.price), "quantity": str(o.quantity),
            "filled_quantity": str(o.filled_quantity), "status": o.status.value,
            "seq": o.seq, "idempotency_key": o.idempotency_key,
        }

    @staticmethod
    def _order_from_state(st: dict) -> Order:
        return Order(
            order_id=st["order_id"], account_id=st["account_id"], market=st["market"],
            side=Side(st["side"]), price=Decimal(st["price"]),
            quantity=Decimal(st["quantity"]),
            filled_quantity=Decimal(st["filled_quantity"]),
            status=OrderStatus(st["status"]), seq=st["seq"],
            idempotency_key=st.get("idempotency_key", ""),
        )

    # ---------- 账户 ----------

    def deposit(self, account_id: str, asset: str, amount: Decimal) -> dict:
        with self._lock:
            amount = self._positive_decimal(amount, "amount")
            self._ledger.credit(account_id, asset, amount)
            self._persist()
            return self.get_account(account_id)

    def get_account(self, account_id: str) -> dict:
        with self._lock:
            snap = self._ledger.snapshot().get(account_id, {})
            return {"account_id": account_id, "balances": snap}

    # ---------- 下单 / 撤单 ----------

    @staticmethod
    def _positive_decimal(value, name: str) -> Decimal:
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise InvalidQuantity(f"{name} is not a valid number: {value!r}")
        if d <= ZERO:
            exc = InvalidPrice if name == "price" else InvalidQuantity
            raise exc(f"{name} must be positive, got {d}")
        return d

    def place_order(self, account_id: str, market: str, side: str,
                    price, quantity, idempotency_key: str) -> dict:
        with self._lock:
            if market not in self._markets:
                raise UnknownMarket(f"unknown market: {market}")
            try:
                side_enum = Side(str(side).upper())
            except ValueError:
                raise InvalidQuantity(f"invalid side: {side!r}")
            price = self._positive_decimal(price, "price")
            quantity = self._positive_decimal(quantity, "quantity")

            request_fp = fingerprint({
                "account_id": account_id, "market": market, "side": side_enum.value,
                "price": str(price), "quantity": str(quantity),
            })
            logger.info(
                "place_order input account=%s market=%s side=%s price=%s qty=%s key=%s",
                account_id, market, side_enum.value, price, quantity, idempotency_key)

            # 幂等前置检查：任何副作用之前先查重，命中则复用/拒绝
            replay = self._idempotency.lookup(idempotency_key, request_fp)
            if replay is not None:
                logger.info("idempotent replay key=%s -> order=%s",
                            idempotency_key, replay["order_id"])
                return replay

            # 风控：暂停 / 参考价缺失、过期、偏离
            self._risk.check_can_match(market, price, time.time())

            # 冻结资金（不足则拒绝）
            cfg = self._markets[market]
            if side_enum is Side.BUY:
                self._ledger.freeze(account_id, cfg.quote_asset, price * quantity)
            else:
                self._ledger.freeze(account_id, cfg.base_asset, quantity)

            self._order_seq += 1
            order = Order(
                order_id=uuid.uuid4().hex, account_id=account_id, market=market,
                side=side_enum, price=price, quantity=quantity,
                seq=self._order_seq, idempotency_key=idempotency_key,
            )
            self._orders[order.order_id] = order
            logger.info("order %s created status=%s", order.order_id, order.status.value)

            trades = self._match(order)
            if order.remaining > ZERO:
                self._books[market].add(order)

            result = self._order_view(order)
            result["trades"] = [self._trade_view(t) for t in trades]
            # 成功后才记录幂等结果（失败请求不占用幂等键）
            self._idempotency.record(idempotency_key, request_fp, result)
            self._ledger.assert_conservation()
            self._persist()
            return result

    def _match(self, taker: Order) -> list[Trade]:
        """价格优先、时间优先撮合；同账户 maker 跳过（不自成交）。"""
        book = self._books[taker.market]
        cfg = self._markets[taker.market]
        trades: list[Trade] = []
        for maker in book.iter_matches(taker.side, taker.price):
            if taker.remaining <= ZERO:
                break
            if maker.account_id == taker.account_id:
                logger.info("self-trade skipped taker=%s maker=%s account=%s",
                            taker.order_id, maker.order_id, maker.account_id)
                continue
            qty = min(taker.remaining, maker.remaining)
            if taker.side is Side.BUY:
                buyer, seller = taker, maker
            else:
                buyer, seller = maker, taker
            # 原子结算：按 maker 价成交，买方按其下单价冻结的价差退回
            self._ledger.settle_trade(
                buyer.account_id, seller.account_id,
                cfg.base_asset, cfg.quote_asset,
                maker.price, qty, buyer.price)
            self._trade_seq += 1
            trade = Trade(
                trade_id=uuid.uuid4().hex, market=taker.market, price=maker.price,
                quantity=qty, maker_order_id=maker.order_id,
                taker_order_id=taker.order_id, maker_account_id=maker.account_id,
                taker_account_id=taker.account_id, seq=self._trade_seq)
            self._trades.append(trade)
            trades.append(trade)
            self._fill(maker, qty)
            self._fill(taker, qty)
            if maker.remaining <= ZERO:
                book.remove(maker.order_id)
            logger.info("trade %s price=%s qty=%s maker=%s taker=%s",
                        trade.trade_id, trade.price, trade.quantity,
                        maker.order_id, taker.order_id)
        return trades

    @staticmethod
    def _fill(order: Order, qty: Decimal) -> None:
        before = order.status.value
        order.filled_quantity += qty
        order.status = (OrderStatus.FILLED if order.remaining <= ZERO
                        else OrderStatus.PARTIALLY_FILLED)
        logger.info("order %s status %s -> %s filled=%s/%s (reason: matched %s)",
                    order.order_id, before, order.status.value,
                    order.filled_quantity, order.quantity, qty)

    def cancel_order(self, account_id: str, order_id: str) -> dict:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise OrderNotFound(f"order not found: {order_id}")
            if order.account_id != account_id:
                raise Forbidden(f"order {order_id} does not belong to {account_id}")
            if not order.is_live:
                raise OrderNotCancellable(
                    f"order {order_id} is {order.status.value}, cannot cancel")
            cfg = self._markets[order.market]
            self._books[order.market].remove(order_id)
            # 解冻剩余冻结额度
            if order.side is Side.BUY:
                self._ledger.unfreeze(account_id, cfg.quote_asset,
                                      order.price * order.remaining)
            else:
                self._ledger.unfreeze(account_id, cfg.base_asset, order.remaining)
            before = order.status.value
            order.status = OrderStatus.CANCELLED
            logger.info("order %s status %s -> CANCELLED (reason: user cancel, "
                        "remaining=%s)", order_id, before, order.remaining)
            self._ledger.assert_conservation()
            self._persist()
            return self._order_view(order)

    # ---------- 查询 ----------

    def get_order(self, order_id: str) -> dict:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise OrderNotFound(f"order not found: {order_id}")
            return self._order_view(order)

    def get_book(self, market: str, levels: int = 10) -> dict:
        with self._lock:
            if market not in self._markets:
                raise UnknownMarket(f"unknown market: {market}")
            return self._books[market].depth(levels)

    def list_trades(self, market: str) -> list[dict]:
        with self._lock:
            return [self._trade_view(t) for t in self._trades if t.market == market]

    # ---------- 管理（需 admin token） ----------

    def _require_admin(self, token: str) -> None:
        if not token:
            raise Unauthorized("admin token required")
        if token != self._admin_token:
            raise Forbidden("invalid admin token")

    def pause(self, token: str) -> dict:
        with self._lock:
            self._require_admin(token)
            self._risk.pause()
            self._persist()
            return {"paused": True}

    def resume(self, token: str) -> dict:
        with self._lock:
            self._require_admin(token)
            self._risk.resume()
            self._persist()
            return {"paused": False}

    def set_risk_params(self, token: str, market: str,
                        max_deviation_bps: int | None = None,
                        reference_ttl_seconds: int | None = None,
                        reference_price=None) -> dict:
        with self._lock:
            self._require_admin(token)
            if market not in self._markets:
                raise UnknownMarket(f"unknown market: {market}")
            if max_deviation_bps is not None or reference_ttl_seconds is not None:
                self._risk.set_params(market, max_deviation_bps, reference_ttl_seconds)
            if reference_price is not None:
                price = self._positive_decimal(reference_price, "price")
                self._risk.set_reference_price(market, price, time.time())
            self._persist()
            return {"market": market, "risk": self._risk.snapshot()}

    # ---------- 视图 ----------

    def _order_view(self, order: Order) -> dict:
        return {
            "order_id": order.order_id, "account_id": order.account_id,
            "market": order.market, "side": order.side.value,
            "price": str(order.price), "quantity": str(order.quantity),
            "filled_quantity": str(order.filled_quantity),
            "remaining": str(order.remaining), "status": order.status.value,
            "seq": order.seq,
        }

    def _trade_view(self, trade: Trade) -> dict:
        return {
            "trade_id": trade.trade_id, "market": trade.market,
            "price": str(trade.price), "quantity": str(trade.quantity),
            "maker_order_id": trade.maker_order_id,
            "taker_order_id": trade.taker_order_id,
            "maker_account_id": trade.maker_account_id,
            "taker_account_id": trade.taker_account_id, "seq": trade.seq,
        }
