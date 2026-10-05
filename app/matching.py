"""Price-time priority matching engine.

Books are in-memory heaps keyed by (price, arrival sequence); they are
rebuilt lazily from durable order state, so a service restart restores
the book. Dead/filled entries are removed lazily. Each fill settles both
accounts and freezes atomically within the service-wide lock.
"""
from __future__ import annotations

import heapq
import logging
from decimal import Decimal
from typing import Callable

from app.models import Fill, LedgerState, Order, OrderStatus, Side, new_fill_id

logger = logging.getLogger("dex.matching")


class MatchingEngine:
    def __init__(self, clock: Callable[[], int] | None = None):
        self.clock = clock or (lambda: 0)
        # market -> (buy_heap, sell_heap); heap items: (sort_key, order_id)
        self._books: dict[str, tuple[list, list]] = {}
        self._rebuilt = False

    # ------------------------------------------------------------------ book

    def _book(self, market: str) -> tuple[list, list]:
        if market not in self._books:
            self._books[market] = ([], [])
        return self._books[market]

    def rebuild(self, state: LedgerState) -> None:
        """Reconstruct heaps from persisted active orders (after restart)."""
        self._books = {}
        for order in state.orders.values():
            if order.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                self._push(order)
        self._rebuilt = True
        logger.info("book rebuilt from ledger: %d active orders resting",
                    sum(len(b) + len(s) for b, s in self._books.values()))

    def _push(self, order: Order) -> None:
        buy_heap, sell_heap = self._book(order.market)
        if order.side is Side.BUY:
            heapq.heappush(buy_heap, ((-order.price, order.sequence), order.order_id))
        else:
            heapq.heappush(sell_heap, ((order.price, order.sequence), order.order_id))

    def add_resting(self, order: Order) -> None:
        logger.info("order rests in book order_id=%s market=%s side=%s price=%s qty=%s",
                    order.order_id, order.market, order.side.value, order.price,
                    order.quantity - order.filled_qty)
        self._push(order)

    # --------------------------------------------------------------- matching

    def match(
        self,
        state: LedgerState,
        taker: Order,
        can_trade_at: Callable[[Decimal], bool] | None = None,
    ) -> list[Fill]:
        """Match taker against the opposite book and settle every fill.

        ``can_trade_at(price)`` gates the risk band; when it rejects the
        best candidate price, matching stops (the book is price-sorted,
        so later candidates can only be worse).
        """
        if not self._rebuilt:
            self.rebuild(state)

        buy_heap, sell_heap = self._book(taker.market)
        heap = sell_heap if taker.side is Side.BUY else buy_heap
        fills: list[Fill] = []
        deferred_self: list = []  # same-account resting entries, re-pushed later

        logger.info("matching taker order_id=%s account=%s side=%s price=%s qty=%s",
                    taker.order_id, taker.account, taker.side.value, taker.price,
                    taker.quantity)

        while taker.filled_qty < taker.quantity:
            # discard dead entries and park same-account orders
            while heap:
                _, oid = heap[0]
                resting = state.orders.get(oid)
                if resting is None or resting.status not in (
                    OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED
                ) or resting.filled_qty >= resting.quantity:
                    heapq.heappop(heap)
                    continue
                if resting.account == taker.account:
                    # self-trade prevention: skip but keep the order valid
                    deferred_self.append(heapq.heappop(heap))
                    logger.info("self-trade prevented: skip resting order_id=%s "
                                "account=%s while matching order_id=%s",
                                oid, resting.account, taker.order_id)
                    continue
                break
            if not heap:
                break

            (sort_price, _seq), maker_id = heap[0]
            maker = state.orders[maker_id]
            if taker.side is Side.BUY:
                cross_price: Decimal = sort_price
                crosses = maker.price <= taker.price
            else:
                cross_price = -sort_price
                crosses = maker.price >= taker.price
            if not crosses:
                logger.info("no further cross: best maker price=%s taker price=%s",
                            maker.price, taker.price)
                break
            if can_trade_at is not None and not can_trade_at(maker.price):
                logger.info("matching stopped by risk band at price=%s "
                            "(taker order_id=%s)", maker.price, taker.order_id)
                break

            qty = min(taker.quantity - taker.filled_qty,
                      maker.quantity - maker.filled_qty)
            price = maker.price  # maker price determines settlement price
            fill = self._settle(state, taker, maker, price, qty)
            fills.append(fill)

            if maker.filled_qty >= maker.quantity:
                heapq.heappop(heap)
                logger.info("maker order fully filled order_id=%s", maker.order_id)
            logger.info("fill executed fill_id=%s price=%s qty=%s buyer=%s seller=%s",
                        fill.fill_id, price, qty, fill.buyer, fill.seller)

        for item in deferred_self:
            heapq.heappush(heap, item)

        return fills

    # -------------------------------------------------------------- settlement

    def _settle(
        self, state: LedgerState, taker: Order, maker: Order,
        price: Decimal, qty: Decimal,
    ) -> Fill:
        cost = price * qty
        if taker.side is Side.BUY:
            buyer_order, seller_order = taker, maker
        else:
            buyer_order, seller_order = maker, taker
        buyer, seller = buyer_order.account, seller_order.account
        base = state.markets[taker.market].base_asset
        quote = state.markets[taker.market].quote_asset

        # ---- buyer side
        # balance includes frozen amounts; settlement spends the actual
        # ``cost`` (maker price), while the reservation for this tranche was
        # ``buyer_order.price * qty`` (the buyer's own limit). Releasing the
        # full reservation immediately returns any price-improvement
        # difference (limit - execution) to the buyer's available balance.
        reserved = buyer_order.price * qty
        bf = state.frozen.get((buyer, quote), Decimal(0)) - reserved
        if bf < 0:
            raise RuntimeError(f"buyer frozen quote would go negative: {buyer}")
        state.frozen[(buyer, quote)] = bf
        state.balances[(buyer, quote)] = (
            state.balances.get((buyer, quote), Decimal(0)) - cost
        )
        if reserved > cost:
            logger.info(
                "price improvement released: buyer=%s order_id=%s "
                "limit=%s exec=%s qty=%s returned=%s %s",
                buyer, buyer_order.order_id, buyer_order.price, price, qty,
                reserved - cost, quote,
            )
        state.balances[(buyer, base)] = (
            state.balances.get((buyer, base), Decimal(0)) + qty
        )

        # ---- seller side: reserved base leaves the seller, quote arrives
        sf = state.frozen.get((seller, base), Decimal(0)) - qty
        if sf < 0:
            raise RuntimeError(f"seller frozen base would go negative: {seller}")
        state.frozen[(seller, base)] = sf
        state.balances[(seller, base)] = (
            state.balances.get((seller, base), Decimal(0)) - qty
        )
        state.balances[(seller, quote)] = (
            state.balances.get((seller, quote), Decimal(0)) + cost
        )

        taker.filled_qty += qty
        maker.filled_qty += qty
        for o in (taker, maker):
            if o.filled_qty >= o.quantity:
                o.status = OrderStatus.FILLED
            elif o.filled_qty > 0:
                o.status = OrderStatus.PARTIALLY_FILLED

        state.fill_sequence += 1
        fill = Fill(
            fill_id=new_fill_id(),
            market=taker.market,
            buy_order_id=buyer_order.order_id,
            sell_order_id=seller_order.order_id,
            taker_order_id=taker.order_id,
            maker_order_id=maker.order_id,
            buyer=buyer,
            seller=seller,
            price=price,
            quantity=qty,
            at_ms=int(self.clock()),
            sequence=state.fill_sequence,
        )
        state.fills.append(fill)
        logger.info(
            "settled: buyer=%s base+=%s quote-=%s quote_frozen-=%s | seller=%s "
            "quote+=%s base_frozen-=%s | taker %s->%s maker %s->%s",
            buyer, qty, cost, reserved, seller, cost, qty,
            taker.order_id, taker.status.value, maker.order_id, maker.status.value,
        )
        return fill
