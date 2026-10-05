"""Core application service.

All mutating operations run under one re-entrant lock, so concurrent
HTTP submissions serialise: no overfill, no double charge, no negative
balance. State is persisted synchronously after each committed command.
"""
from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal
from typing import Any, Callable

from app.errors import DexError
from app.matching import MatchingEngine
from app.models import (
    ErrorCode,
    LedgerState,
    MarketConfig,
    Order,
    OrderStatus,
    ReferencePrice,
    RiskParams,
    Side,
    new_order_id,
)
from app.storage import load_state, save_state

logger = logging.getLogger("dex.service")

ZERO = Decimal(0)


class DexService:
    def __init__(
        self,
        ledger_path: str | None = None,
        clock: Callable[[], int] | None = None,
        admin_token: str = "admin-token",
    ):
        self.ledger_path = ledger_path
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.admin_token = admin_token
        self._lock = threading.RLock()
        self.engine = MatchingEngine(clock=self.clock)
        self.state = load_state(ledger_path) if ledger_path else None
        if self.state is None:
            self.state = LedgerState()
            logger.info("fresh ledger initialised")
            self._seed_defaults()
            self._persist()
        else:
            logger.info("ledger loaded: %d orders, %d fills",
                        len(self.state.orders), len(self.state.fills))
        self.engine.rebuild(self.state)

    # ------------------------------------------------------------ bootstrap

    def _seed_defaults(self) -> None:
        cfg = MarketConfig(symbol="BTC-USDC", base_asset="BTC", quote_asset="USDC")
        self.state.markets[cfg.symbol] = cfg
        self.state.risk[cfg.symbol] = RiskParams()
        self.state.halted[cfg.symbol] = False

    def _persist(self) -> None:
        if self.ledger_path:
            save_state(self.ledger_path, self.state)

    def require_admin(self, token: str) -> None:
        if token != self.admin_token:
            raise DexError(ErrorCode.UNAUTHORIZED, "invalid or missing admin token")

    # ------------------------------------------------------------- accounts

    def register_market(self, config: MarketConfig) -> None:
        with self._lock:
            self.state.markets[config.symbol] = config
            self.state.risk.setdefault(config.symbol, RiskParams())
            self.state.halted.setdefault(config.symbol, False)
            self._persist()
            logger.info("market registered symbol=%s base=%s quote=%s",
                        config.symbol, config.base_asset, config.quote_asset)

    def credit(self, account: str, asset: str, amount: Decimal) -> None:
        with self._lock:
            if amount <= ZERO:
                raise DexError(ErrorCode.INVALID_QUANTITY,
                               f"credit amount must be positive, got {amount}")
            key = (account, asset)
            self.state.balances[key] = self.state.balances.get(key, ZERO) + amount
            self.state.credited[asset] = self.state.credited.get(asset, ZERO) + amount
            logger.info("credit account=%s asset=%s amount=%s new_balance=%s",
                        account, asset, amount, self.state.balances[key])
            self._persist()

    # ------------------------------------------------------------ risk/admin

    def set_reference_price(self, market: str, price: Decimal) -> None:
        with self._lock:
            self._require_market(market)
            if price <= ZERO or not price.is_finite():
                raise DexError(ErrorCode.INVALID_PRICE, f"bad reference price {price}")
            now = int(self.clock())
            self.state.references[market] = ReferencePrice(price=price, at_ms=now)
            logger.info("reference price set market=%s price=%s at_ms=%d",
                        market, price, now)
            self._persist()

    def set_risk_params(self, market: str, params: RiskParams) -> None:
        with self._lock:
            self._require_market(market)
            if params.max_deviation_bps < 0 or params.reference_max_age_ms < 0:
                raise DexError(ErrorCode.INVALID_RISK_PARAMS,
                               "risk parameters must be non-negative")
            self.state.risk[market] = params
            logger.info("risk params updated market=%s max_dev_bps=%d max_age_ms=%d",
                        market, params.max_deviation_bps, params.reference_max_age_ms)
            self._persist()

    def set_halted(self, market: str, halted: bool) -> None:
        with self._lock:
            self._require_market(market)
            self.state.halted[market] = halted
            logger.info("market=%s trading %s", market,
                        "halted" if halted else "resumed")
            self._persist()

    # -------------------------------------------------------------- trading

    def place_order(self, req: Any) -> dict:
        with self._lock:
            now = int(self.clock())
            fingerprint = {
                "type": "place_order",
                "market": str(req.market),
                "account": str(req.account),
                "side": req.side.value if isinstance(req.side, Side) else str(req.side),
                "price": str(req.price),
                "quantity": str(req.quantity),
            }
            cached = self._idempotency_check(req.idempotency_key, fingerprint)
            if cached is not None:
                logger.info("idempotent replay key=%s -> order_id=%s",
                            req.idempotency_key, cached.get("order_id"))
                return cached

            market = self._require_market(req.market)

            # ---- validation (distinguishable errors)
            if not isinstance(req.quantity, Decimal):
                req.quantity = Decimal(str(req.quantity))
            if not isinstance(req.price, Decimal):
                req.price = Decimal(str(req.price))
            if req.quantity <= ZERO or req.quantity % market.lot_size != ZERO:
                raise DexError(
                    ErrorCode.INVALID_QUANTITY,
                    f"quantity {req.quantity} must be positive and a multiple "
                    f"of lot_size {market.lot_size}",
                )
            if req.price <= ZERO or req.price % market.tick_size != ZERO:
                raise DexError(
                    ErrorCode.INVALID_PRICE,
                    f"price {req.price} must be positive and a multiple "
                    f"of tick_size {market.tick_size}",
                )

            side = req.side if isinstance(req.side, Side) else Side(req.side)
            asset = market.quote_asset if side is Side.BUY else market.base_asset
            need = req.price * req.quantity if side is Side.BUY else req.quantity
            key = (req.account, asset)
            available = self.state.balances.get(key, ZERO) - self.state.frozen.get(key, ZERO)
            logger.info(
                "place_order account=%s side=%s needs=%s %s available=%s "
                "(balance=%s frozen=%s)",
                req.account, side.value, need, asset, available,
                self.state.balances.get(key, ZERO), self.state.frozen.get(key, ZERO),
            )
            if available < need:
                raise DexError(
                    ErrorCode.INSUFFICIENT_BALANCE,
                    f"account {req.account} needs {need} {asset} but only "
                    f"{available} available",
                )
            # a negative frozen value means internal corruption, surface distinctly
            if self.state.frozen.get(key, ZERO) < ZERO:
                raise DexError(ErrorCode.INSUFFICIENT_FROZEN,
                               f"frozen {asset} for {req.account} is negative")

            self.state.order_sequence += 1
            order = Order(
                order_id=new_order_id(),
                account=req.account,
                market=req.market,
                side=side,
                price=req.price,
                quantity=req.quantity,
                status=OrderStatus.NEW,
                created_at_ms=now,
                sequence=self.state.order_sequence,
            )
            self.state.orders[order.order_id] = order
            self.state.frozen[key] = self.state.frozen.get(key, ZERO) + need
            logger.info("order accepted order_id=%s seq=%d; froze %s %s",
                        order.order_id, order.sequence, need, asset)

            blocked_reason: str | None = None
            fills: list = []
            if self.state.halted.get(req.market, False):
                blocked_reason = ErrorCode.MARKET_HALTED.value
                logger.info("market halted: order %s rests without matching",
                            order.order_id)
            else:
                gate, reason = self._risk_gate(req.market, now)
                if gate is None:
                    blocked_reason = reason
                    logger.info("reference gate blocked (%s): order %s rests",
                                reason, order.order_id)
                else:
                    fills = self.engine.match(self.state, order, can_trade_at=gate)

            # unfilled/partially filled orders always enter the book, even
            # when matching was blocked, so they remain effective once
            # trading resumes (without requiring a restart).
            if order.filled_qty < order.quantity:
                self.engine.add_resting(order)

            result = self._order_view(order, fills=fills, blocked_reason=blocked_reason)
            self._store_idempotent(req.idempotency_key, "place_order", result, fingerprint)
            self._persist()
            logger.info("place_order done order_id=%s status=%s filled=%s fills=%d",
                        order.order_id, order.status.value, order.filled_qty, len(fills))
            return result

    def cancel_order(self, req: Any) -> dict:
        with self._lock:
            fingerprint = {
                "type": "cancel_order",
                "order_id": str(req.order_id),
                "account": str(req.account),
            }
            cached = self._idempotency_check(req.idempotency_key, fingerprint)
            if cached is not None:
                logger.info("idempotent replay key=%s cancel order_id=%s",
                            req.idempotency_key, req.order_id)
                return cached

            order = self.state.orders.get(req.order_id)
            if order is None:
                raise DexError(ErrorCode.ORDER_NOT_ACTIVE,
                               f"order {req.order_id} not found")
            if order.account != req.account:
                raise DexError(ErrorCode.ORDER_NOT_ACTIVE,
                               "order belongs to another account")
            if order.status not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                raise DexError(ErrorCode.ORDER_NOT_ACTIVE,
                               f"order {req.order_id} is {order.status.value}, "
                               "cannot cancel")

            release = self._release_and_cancel(order)
            logger.info(
                "order cancelled order_id=%s account=%s released=%s %s "
                "filled_qty=%s status NEW->CANCELLED",
                order.order_id, order.account, release[1], release[0],
                order.filled_qty,
            )
            result = self._order_view(order)
            self._store_idempotent(req.idempotency_key, "cancel_order", result, fingerprint)
            self._persist()
            return result

    # ------------------------------------------------------- account funds

    def get_funds(self, account: str, market: str | None = None) -> dict:
        """Per-order locked-funds breakdown plus per-asset totals.

        The locked rows are derived from the live order state, and the
        per-asset ``frozen`` totals are the ledger's own counters; the
        reconciliation endpoint verifies the two always agree.
        """
        with self._lock:
            if market is not None:
                self._require_market(market)
            locked: list[dict] = []
            for order in self.state.orders.values():
                if order.account != account:
                    continue
                if market is not None and order.market != market:
                    continue
                if order.status not in (OrderStatus.NEW,
                                        OrderStatus.PARTIALLY_FILLED):
                    continue
                cfg = self.state.markets[order.market]
                remaining = order.quantity - order.filled_qty
                if order.side is Side.BUY:
                    asset, amount = cfg.quote_asset, remaining * order.price
                else:
                    asset, amount = cfg.base_asset, remaining
                locked.append({
                    "order_id": order.order_id,
                    "market": order.market,
                    "side": order.side.value,
                    "price": str(order.price),
                    "remaining_quantity": str(remaining),
                    "asset": asset,
                    "locked_amount": str(amount),
                })
            locked.sort(key=lambda row: row["order_id"])

            assets: list[dict] = []
            seen = {a for (acc, a) in self.state.balances if acc == account}
            seen |= {a for (acc, a) in self.state.frozen if acc == account}
            seen |= {row["asset"] for row in locked}
            for asset in sorted(seen):
                balance = self.state.balances.get((account, asset), ZERO)
                frozen = self.state.frozen.get((account, asset), ZERO)
                assets.append({
                    "asset": asset,
                    "balance": str(balance),
                    "frozen": str(frozen),
                    "available": str(balance - frozen),
                })
            logger.info("funds query account=%s market=%s locked_orders=%d",
                        account, market, len(locked))
            return {"account": account, "market": market,
                    "locked": locked, "assets": assets}

    # ---------------------------------------------------------- batch cancel

    def cancel_batch(self, req: Any) -> dict:
        """Atomically cancel an explicit list of orders (all-or-nothing).

        Every item is validated before any fund is released: a missing,
        foreign, already-finished or duplicated order id aborts the whole
        batch with INVALID_BATCH and the account's funds stay untouched.
        """
        with self._lock:
            order_ids = [str(oid) for oid in req.order_ids]
            fingerprint = {
                "type": "cancel_batch",
                "account": str(req.account),
                "order_ids": list(order_ids),
            }
            cached = self._idempotency_check(req.idempotency_key, fingerprint)
            if cached is not None:
                logger.info("idempotent replay key=%s cancel_batch account=%s",
                            req.idempotency_key, req.account)
                return cached

            if not order_ids:
                raise DexError(ErrorCode.INVALID_BATCH,
                               "order_ids must be a non-empty list")

            # ---- validate everything first; nothing is mutated in this pass
            seen: set[str] = set()
            targets: list[Order] = []
            for idx, oid in enumerate(order_ids):
                if oid in seen:
                    raise DexError(
                        ErrorCode.INVALID_BATCH,
                        f"item[{idx}] order {oid}: duplicated in request; "
                        "whole batch rejected, no funds changed",
                    )
                seen.add(oid)
                order = self.state.orders.get(oid)
                if order is None:
                    raise DexError(
                        ErrorCode.INVALID_BATCH,
                        f"item[{idx}] order {oid}: not found; "
                        "whole batch rejected, no funds changed",
                    )
                if order.account != req.account:
                    raise DexError(
                        ErrorCode.INVALID_BATCH,
                        f"item[{idx}] order {oid}: belongs to another account; "
                        "whole batch rejected, no funds changed",
                    )
                if order.status not in (OrderStatus.NEW,
                                        OrderStatus.PARTIALLY_FILLED):
                    raise DexError(
                        ErrorCode.INVALID_BATCH,
                        f"item[{idx}] order {oid}: already {order.status.value}; "
                        "whole batch rejected, no funds changed",
                    )
                targets.append(order)

            # ---- all items valid: cancel every one (single lock => atomic)
            results = []
            for order in targets:
                asset, released = self._release_and_cancel(order)
                logger.info("batch cancel order_id=%s released=%s %s",
                            order.order_id, released, asset)
                results.append({
                    "order_id": order.order_id,
                    "result": "cancelled",
                    "asset": asset,
                    "released_amount": str(released),
                })
            result = {
                "account": str(req.account),
                "results": results,
                "cancelled_count": len(results),
                "skipped_count": 0,
            }
            self._store_idempotent(req.idempotency_key, "cancel_batch",
                                   result, fingerprint)
            self._persist()
            logger.info("cancel_batch done account=%s cancelled=%d",
                        req.account, len(results))
            return result

    def cancel_scope(self, req: Any) -> dict:
        """Cancel every still-active order of an account (optionally per market).

        Orders in scope that are no longer active are reported as skipped
        (never as failures). One pass under the service lock, one persist.
        """
        with self._lock:
            market = getattr(req, "market", None)
            fingerprint = {
                "type": "cancel_scope",
                "account": str(req.account),
                "market": str(market) if market else None,
            }
            cached = self._idempotency_check(req.idempotency_key, fingerprint)
            if cached is not None:
                logger.info("idempotent replay key=%s cancel_scope account=%s",
                            req.idempotency_key, req.account)
                return cached

            if market is not None:
                self._require_market(market)

            results = []
            cancelled = skipped = 0
            for order in sorted(self.state.orders.values(),
                                key=lambda o: o.sequence):
                if order.account != req.account:
                    continue
                if market is not None and order.market != market:
                    continue
                if order.status in (OrderStatus.NEW,
                                    OrderStatus.PARTIALLY_FILLED):
                    asset, released = self._release_and_cancel(order)
                    cancelled += 1
                    results.append({
                        "order_id": order.order_id,
                        "market": order.market,
                        "result": "cancelled",
                        "asset": asset,
                        "released_amount": str(released),
                    })
                else:
                    skipped += 1
                    results.append({
                        "order_id": order.order_id,
                        "market": order.market,
                        "result": "skipped",
                        "reason": f"order already {order.status.value}",
                    })
            result = {
                "account": str(req.account),
                "market": market,
                "results": results,
                "cancelled_count": cancelled,
                "skipped_count": skipped,
            }
            self._store_idempotent(req.idempotency_key, "cancel_scope",
                                   result, fingerprint)
            self._persist()
            logger.info("cancel_scope done account=%s market=%s cancelled=%d "
                        "skipped=%d", req.account, market, cancelled, skipped)
            return result

    # ---------------------------------------------------------- reconciliation

    def reconcile(self) -> dict:
        """Read-only whole-ledger consistency check.

        Verifies, without touching any state (and independently of market
        halts or reference prices):

        1. asset conservation: per asset, sum of all account balances
           equals the total ever credited;
        2. frozen consistency: per account/asset, the frozen counter
           equals the sum of reservations of that account's active orders;
        3. no negative balance or frozen counter anywhere.
        """
        with self._lock:
            discrepancies: list[dict] = []

            # ---- expected reservations derived from active orders
            expected_frozen: dict[tuple[str, str], Decimal] = {}
            for order in self.state.orders.values():
                if order.status not in (OrderStatus.NEW,
                                        OrderStatus.PARTIALLY_FILLED):
                    continue
                cfg = self.state.markets[order.market]
                remaining = order.quantity - order.filled_qty
                if order.side is Side.BUY:
                    key = (order.account, cfg.quote_asset)
                    amount = remaining * order.price
                else:
                    key = (order.account, cfg.base_asset)
                    amount = remaining
                expected_frozen[key] = expected_frozen.get(key, ZERO) + amount

            # ---- frozen consistency per account/asset
            frozen_keys = set(expected_frozen) | set(self.state.frozen)
            for key in sorted(frozen_keys):
                account, asset = key
                expected = expected_frozen.get(key, ZERO)
                actual = self.state.frozen.get(key, ZERO)
                if actual != expected:
                    discrepancies.append({
                        "kind": "frozen_mismatch",
                        "account": account,
                        "asset": asset,
                        "expected": str(expected),
                        "actual": str(actual),
                    })

            # ---- asset conservation: balances vs total credited
            totals: dict[str, Decimal] = {}
            for (_account, asset), v in self.state.balances.items():
                totals[asset] = totals.get(asset, ZERO) + v
            for asset in sorted(set(totals) | set(self.state.credited)):
                expected = self.state.credited.get(asset, ZERO)
                actual = totals.get(asset, ZERO)
                if actual != expected:
                    discrepancies.append({
                        "kind": "asset_conservation",
                        "asset": asset,
                        "expected": str(expected),
                        "actual": str(actual),
                    })

            # ---- no negative counters
            for (account, asset), v in sorted(self.state.balances.items()):
                if v < ZERO:
                    discrepancies.append({
                        "kind": "negative_balance",
                        "account": account,
                        "asset": asset,
                        "expected": ">= 0",
                        "actual": str(v),
                    })
            for (account, asset), v in sorted(self.state.frozen.items()):
                if v < ZERO:
                    discrepancies.append({
                        "kind": "negative_frozen",
                        "account": account,
                        "asset": asset,
                        "expected": ">= 0",
                        "actual": str(v),
                    })

            accounts = {acc for (acc, _a) in self.state.balances}
            accounts |= {acc for (acc, _a) in self.state.frozen}
            ok = not discrepancies
            logger.info("reconcile ok=%s accounts=%d discrepancies=%d",
                        ok, len(accounts), len(discrepancies))
            return {
                "ok": ok,
                "checked_accounts": len(accounts),
                "checked_assets": sorted(set(totals) | set(self.state.credited)),
                "discrepancies": discrepancies,
            }

    # -------------------------------------------------------------- queries

    def get_order(self, order_id: str) -> dict:
        with self._lock:
            order = self.state.orders.get(order_id)
            if order is None:
                raise DexError(ErrorCode.ORDER_NOT_ACTIVE,
                               f"order {order_id} not found")
            return self._order_view(order)

    def get_balance(self, account: str, asset: str) -> dict:
        with self._lock:
            balance = self.state.balances.get((account, asset), ZERO)
            frozen = self.state.frozen.get((account, asset), ZERO)
            return {
                "account": account,
                "asset": asset,
                "balance": str(balance),
                "frozen": str(frozen),
                "available": str(balance - frozen),
            }

    def get_snapshot(self, market: str | None = None) -> dict:
        with self._lock:
            markets = [market] if market else list(self.state.markets)
            out: dict[str, Any] = {"markets": {}}
            for sym in markets:
                if sym not in self.state.markets:
                    raise DexError(ErrorCode.UNKNOWN_MARKET, f"unknown market {sym}")
                bids: dict[str, str] = {}
                asks: dict[str, str] = {}
                for o in self.state.orders.values():
                    if o.market != sym:
                        continue
                    if o.status not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                        continue
                    rem = o.quantity - o.filled_qty
                    book = bids if o.side is Side.BUY else asks
                    p = str(o.price)
                    book[p] = str(Decimal(book.get(p, "0")) + rem)
                ref = self.state.references.get(sym)
                out["markets"][sym] = {
                    "halted": self.state.halted.get(sym, False),
                    "reference": (
                        {"price": str(ref.price), "at_ms": ref.at_ms} if ref else None
                    ),
                    "risk": {
                        "max_deviation_bps": self.state.risk[sym].max_deviation_bps,
                        "reference_max_age_ms": self.state.risk[sym].reference_max_age_ms,
                    },
                    "bids": [{"price": p, "quantity": bids[p]}
                             for p in sorted(bids, key=Decimal, reverse=True)],
                    "asks": [{"price": p, "quantity": asks[p]}
                             for p in sorted(asks, key=Decimal)],
                }
            return out

    # -------------------------------------------------------------- helpers

    def _release_and_cancel(self, order: Order) -> tuple[str, Decimal]:
        """Release the order's remaining reservation and mark it CANCELLED.

        Returns ``(asset, released_amount)``. Caller must hold the lock and
        must have verified the order is still active.
        """
        market = self.state.markets[order.market]
        remaining = order.quantity - order.filled_qty
        if order.side is Side.BUY:
            asset, release = market.quote_asset, remaining * order.price
        else:
            asset, release = market.base_asset, remaining
        key = (order.account, asset)
        frozen = self.state.frozen.get(key, ZERO)
        if frozen < release:
            # should never happen with correct settlement; reported distinctly
            raise DexError(
                ErrorCode.INSUFFICIENT_FROZEN,
                f"cannot release {release} {asset}, frozen={frozen} "
                "(ledger corruption?)",
            )
        self.state.frozen[key] = frozen - release
        order.status = OrderStatus.CANCELLED
        return asset, release

    def _require_market(self, symbol: str) -> MarketConfig:
        cfg = self.state.markets.get(symbol)
        if cfg is None:
            raise DexError(ErrorCode.UNKNOWN_MARKET, f"unknown market {symbol}")
        return cfg

    def _risk_gate(
        self, market: str, now: int,
    ) -> tuple[Callable[[Decimal], bool] | None, str | None]:
        """Return a band predicate, or (None, reason) if trading is blocked."""
        ref = self.state.references.get(market)
        if ref is None:
            return None, ErrorCode.REFERENCE_UNAVAILABLE.value
        risk = self.state.risk[market]
        if now - ref.at_ms > risk.reference_max_age_ms:
            logger.info("reference stale: age_ms=%d max=%d",
                        now - ref.at_ms, risk.reference_max_age_ms)
            return None, ErrorCode.REFERENCE_STALE.value

        def gate(price: Decimal, ref_price: Decimal = ref.price,
                 bps: int = risk.max_deviation_bps) -> bool:
            allowed = ref_price * bps / Decimal(10000)
            ok = abs(price - ref_price) <= allowed
            if not ok:
                logger.warning("price %s deviates from ref %s beyond %d bps",
                               price, ref_price, bps)
            return ok

        return gate, None

    def _idempotency_check(
        self, key: str | None, fingerprint: dict,
    ) -> dict | None:
        if not key:
            return None
        existing = self.state.idempotency.get(key)
        if existing is None:
            return None
        _kind, payload = existing
        if payload.get("_fingerprint") != fingerprint:
            raise DexError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                f"idempotency key {key!r} was already used with different "
                "request content",
            )
        result = {k: v for k, v in payload.items() if k != "_fingerprint"}
        return result

    def _order_view(self, order: Order, fills: list | None = None,
                    blocked_reason: str | None = None) -> dict:
        view = {
            "order_id": order.order_id,
            "account": order.account,
            "market": order.market,
            "side": order.side.value,
            "price": str(order.price),
            "quantity": str(order.quantity),
            "filled_quantity": str(order.filled_qty),
            "remaining_quantity": str(order.quantity - order.filled_qty),
            "status": order.status.value,
            "created_at_ms": order.created_at_ms,
            "sequence": order.sequence,
        }
        if fills is not None:
            view["fills"] = [
                {
                    "fill_id": f.fill_id,
                    "price": str(f.price),
                    "quantity": str(f.quantity),
                    "maker_order_id": f.maker_order_id,
                    "taker_order_id": f.taker_order_id,
                    "counterparty": f.seller if order.side is Side.BUY else f.buyer,
                }
                for f in fills
            ]
        if blocked_reason:
            view["blocked_reason"] = blocked_reason
        return view

    def _store_idempotent(self, key: str | None, kind: str, view: dict,
                          fingerprint: dict) -> None:
        if not key:
            return
        stored = dict(view)
        stored.pop("_fingerprint", None)
        stored["_fingerprint"] = fingerprint
        self.state.idempotency[key] = (kind, stored)
