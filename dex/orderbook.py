"""订单簿：价格优先、时间优先的限价单撮合。

实现：每个价位一个 FIFO 队列（dict 保序），价位层用排序列表维护。
"""
from __future__ import annotations

import bisect
import logging
from decimal import Decimal
from typing import Iterator

from .models import Order, Side

logger = logging.getLogger("dex.orderbook")


class OrderBook:
    def __init__(self, market: str) -> None:
        self.market = market
        self._orders: dict[str, Order] = {}
        # 价位 -> {order_id: Order}（dict 保持插入序，即时间优先）
        self._bids: dict[Decimal, dict[str, Order]] = {}
        self._asks: dict[Decimal, dict[str, Order]] = {}
        self._bid_prices: list[Decimal] = []   # 升序
        self._ask_prices: list[Decimal] = []   # 升序

    def _side_maps(self, side: Side):
        if side is Side.BUY:
            return self._bids, self._bid_prices
        return self._asks, self._ask_prices

    def add(self, order: Order) -> None:
        levels, prices = self._side_maps(order.side)
        if order.price not in levels:
            bisect.insort(prices, order.price)
            levels[order.price] = {}
        levels[order.price][order.order_id] = order
        self._orders[order.order_id] = order
        logger.info("book add market=%s order=%s side=%s price=%s qty=%s seq=%s",
                    self.market, order.order_id, order.side.value,
                    order.price, order.remaining, order.seq)

    def remove(self, order_id: str) -> Order | None:
        order = self._orders.pop(order_id, None)
        if order is None:
            return None
        levels, prices = self._side_maps(order.side)
        level = levels.get(order.price)
        if level is not None:
            level.pop(order_id, None)
            if not level:
                del levels[order.price]
                prices.remove(order.price)
        logger.info("book remove market=%s order=%s", self.market, order_id)
        return order

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def best_bid(self) -> Decimal | None:
        return self._bid_prices[-1] if self._bid_prices else None

    def best_ask(self) -> Decimal | None:
        return self._ask_prices[0] if self._ask_prices else None

    def iter_matches(self, taker_side: Side, limit_price: Decimal) -> Iterator[Order]:
        """按价格优先、时间优先枚举 taker 可成交的 maker 订单（快照顺序）。

        taker 为 BUY 时吃 asks（价低优先），为 SELL 时吃 bids（价高优先）。
        """
        if taker_side is Side.BUY:
            prices = [p for p in self._ask_prices if p <= limit_price]
            levels = self._asks
        else:
            prices = [p for p in self._bid_prices if p >= limit_price]
            levels = self._bids
        for price in prices:
            for order in list(levels[price].values()):
                yield order

    def depth(self, levels: int = 10) -> dict:
        def aggregate(level_map, prices):
            return [
                {"price": str(p),
                 "quantity": str(sum((o.remaining for o in level_map[p].values()), Decimal(0)))}
                for p in prices[:levels]
            ]
        return {
            "market": self.market,
            "bids": aggregate(self._bids, list(reversed(self._bid_prices))),
            "asks": aggregate(self._asks, self._ask_prices),
        }
