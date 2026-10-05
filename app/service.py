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

            result = self._cancel_active_order(order)
            view = self._order_view(order)
            self._store_idempotent(req.idempotency_key, "cancel_order", view, fingerprint)
            self._persist()
            return view

    def _cancel_active_order(self, order: Order) -> dict:
        """Release the remaining reservation of an active order and mark it
        CANCELLED. Caller must hold the lock and must have verified the
        order is still active. Returns release details."""
        market = self.state.markets[order.market]
        remaining = order.quantity - order.filled_qty
        if order.side is Side.BUY:
            asset = market.quote_asset
            release = remaining * order.price
        else:
            asset = market.base_asset
            release = remaining
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
        logger.info(
            "order cancelled order_id=%s account=%s released=%s %s "
            "filled_qty=%s status NEW->CANCELLED",
            order.order_id, order.account, release, asset, order.filled_qty,
        )
        return {"asset": asset, "released": release}

    def batch_cancel(self, req: Any) -> dict:
        """Cancel many orders of one account atomically.

        Two modes:
        - explicit (``order_ids`` given): the whole batch is validated
          first; any unknown / foreign / terminated / duplicate id (or an
          order outside ``market``) aborts the entire batch — no funds move
          — and the offending items are reported per entry.
        - scope (``order_ids`` is None): cancels every currently active
          order of the account (optionally restricted to ``market``);
          orders in scope that are already terminated are reported as
          ``skipped`` and are not an error.

        The whole batch runs under the service lock, so it cannot
        interleave with placements or fills: no half-released state.
        """
        with self._lock:
            fingerprint = {
                "type": "batch_cancel",
                "account": str(req.account),
                "market": str(req.market) if req.market else None,
                "order_ids": sorted(req.order_ids) if req.order_ids else None,
            }
            cached = self._idempotency_check(req.idempotency_key, fingerprint)
            if cached is not None:
                logger.info("idempotent replay key=%s batch_cancel account=%s",
                            req.idempotency_key, req.account)
                return cached

            if req.market is not None:
                self._require_market(req.market)

            if req.order_ids:
                result = self._batch_cancel_explicit(req)
            else:
                result = self._batch_cancel_scope(req)

            self._store_idempotent(req.idempotency_key, "batch_cancel",
                                   result, fingerprint)
            self._persist()
            logger.info("batch_cancel account=%s market=%s applied=%s "
                        "cancelled=%d skipped=%d failed=%d",
                        req.account, req.market, result["applied"],
                        result["cancelled"], result["skipped"], result["failed"])
            return result

    def _batch_cancel_explicit(self, req: Any) -> dict:
        problems: dict[str, str] = {}
        seen: set[str] = set()
        orders: dict[str, Order] = {}
        for oid in req.order_ids:
            if oid in seen:
                problems[oid] = "duplicate order_id in request"
                continue
            seen.add(oid)
            order = self.state.orders.get(oid)
            if order is None:
                problems[oid] = "order not found"
            elif order.account != req.account:
                problems[oid] = "order belongs to another account"
            elif req.market is not None and order.market != req.market:
                problems[oid] = f"order is in market {order.market}, not {req.market}"
            elif order.status not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                problems[oid] = f"order already {order.status.value}"
            else:
                orders[oid] = order

        if problems:
            logger.warning("batch_cancel aborted account=%s problems=%s",
                           req.account, problems)
            results = []
            for oid in req.order_ids:
                if oid in problems:
                    results.append({"order_id": oid, "status": "failed",
                                    "reason": problems[oid]})
                else:
                    results.append({"order_id": oid, "status": "skipped",
                                    "reason": "batch_aborted"})
            return self._batch_result(req, applied=False, results=results)

        results = []
        for oid in req.order_ids:
            info = self._cancel_active_order(orders[oid])
            results.append({
                "order_id": oid,
                "status": "cancelled",
                "asset": info["asset"],
                "released": str(info["released"]),
            })
        return self._batch_result(req, applied=True, results=results)

    def _batch_cancel_scope(self, req: Any) -> dict:
        results = []
        in_scope = [
            o for o in self.state.orders.values()
            if o.account == req.account
            and (req.market is None or o.market == req.market)
        ]
        in_scope.sort(key=lambda o: o.sequence)
        for order in in_scope:
            if order.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                info = self._cancel_active_order(order)
                results.append({
                    "order_id": order.order_id,
                    "status": "cancelled",
                    "asset": info["asset"],
                    "released": str(info["released"]),
                })
            else:
                results.append({
                    "order_id": order.order_id,
                    "status": "skipped",
                    "reason": f"order already {order.status.value}",
                })
        return self._batch_result(req, applied=True, results=results)

    @staticmethod
    def _batch_result(req: Any, applied: bool, results: list[dict]) -> dict:
        return {
            "account": req.account,
            "market": req.market,
            "applied": applied,
            "cancelled": sum(1 for r in results if r["status"] == "cancelled"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "results": results,
        }

    # -------------------------------------------------------------- queries

    def reconcile(self) -> dict:
        """Read-only whole-ledger audit.

        Checks, without touching any state (and independent of market
        halt / reference-price conditions):
        1. conservation: per asset, sum of all balances == total credited;
        2. per (account, asset): frozen >= 0, balance >= 0,
           frozen <= balance;
        3. per (account, asset): frozen == sum of remaining reservations
           of that account's active orders in that asset.
        Every mismatch is reported with account, asset, expected and
        actual values.
        """
        with self._lock:
            discrepancies: list[dict] = []

            # expected lockups derived from active orders
            expected_locks: dict[tuple[str, str], Decimal] = {}
            for o in self.state.orders.values():
                if o.status not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                    continue
                remaining = o.quantity - o.filled_qty
                if remaining <= ZERO:
                    continue
                cfg = self.state.markets[o.market]
                if o.side is Side.BUY:
                    key = (o.account, cfg.quote_asset)
                    amount = remaining * o.price
                else:
                    key = (o.account, cfg.base_asset)
                    amount = remaining
                expected_locks[key] = expected_locks.get(key, ZERO) + amount

            # 1. conservation per asset
            totals: dict[str, Decimal] = {}
            for (_, asset), v in self.state.balances.items():
                totals[asset] = totals.get(asset, ZERO) + v
            for asset in sorted(set(totals) | set(self.state.credited)):
                expected = self.state.credited.get(asset, ZERO)
                actual = totals.get(asset, ZERO)
                if actual != expected:
                    discrepancies.append({
                        "kind": "ASSET_NOT_CONSERVED",
                        "account": None,
                        "asset": asset,
                        "expected": str(expected),
                        "actual": str(actual),
                    })

            # 2./3. per-account frozen integrity
            accounts = {acc for (acc, _) in self.state.balances}
            accounts |= {acc for (acc, _) in self.state.frozen}
            accounts |= {acc for (acc, _) in expected_locks}
            keys = set(self.state.frozen) | set(expected_locks)
            for key in sorted(keys):
                account, asset = key
                frozen = self.state.frozen.get(key, ZERO)
                expected = expected_locks.get(key, ZERO)
                balance = self.state.balances.get(key, ZERO)
                if frozen < ZERO:
                    discrepancies.append({
                        "kind": "NEGATIVE_FROZEN",
                        "account": account,
                        "asset": asset,
                        "expected": "0",
                        "actual": str(frozen),
                    })
                if balance < ZERO:
                    discrepancies.append({
                        "kind": "NEGATIVE_BALANCE",
                        "account": account,
                        "asset": asset,
                        "expected": "0",
                        "actual": str(balance),
                    })
                if frozen > balance:
                    discrepancies.append({
                        "kind": "FROZEN_EXCEEDS_BALANCE",
                        "account": account,
                        "asset": asset,
                        "expected": str(balance),
                        "actual": str(frozen),
                    })
                if frozen != expected:
                    discrepancies.append({
                        "kind": "FROZEN_MISMATCH",
                        "account": account,
                        "asset": asset,
                        "expected": str(expected),
                        "actual": str(frozen),
                    })

            ok = not discrepancies
            logger.info("reconcile ok=%s accounts=%d discrepancies=%d",
                        ok, len(accounts), len(discrepancies))
            return {
                "ok": ok,
                "checked_accounts": len(accounts),
                "checked_assets": sorted(set(totals) | set(self.state.credited)),
                "discrepancies": discrepancies,
            }

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

    def get_locks(self, account: str, market: str | None = None) -> dict:
        """Per-order lockup detail for an account, optionally one market.

        Every active (NEW / PARTIALLY_FILLED) order contributes exactly one
        entry: its remaining reservation. Filled/cancelled orders lock
        nothing. ``totals`` always reports account-wide balance/frozen/
        available per asset, plus ``locked`` = the sum of the listed locks
        per asset, so the list can be reconciled against actual frozen
        amounts at any time (with no market filter, locked == frozen).
        """
        with self._lock:
            if market is not None:
                self._require_market(market)
            locks: list[dict] = []
            locked_totals: dict[str, Decimal] = {}
            for o in self.state.orders.values():
                if o.account != account:
                    continue
                if market is not None and o.market != market:
                    continue
                if o.status not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                    continue
                remaining = o.quantity - o.filled_qty
                if remaining <= ZERO:
                    continue
                cfg = self.state.markets[o.market]
                if o.side is Side.BUY:
                    asset, amount = cfg.quote_asset, remaining * o.price
                else:
                    asset, amount = cfg.base_asset, remaining
                locks.append({
                    "order_id": o.order_id,
                    "market": o.market,
                    "side": o.side.value,
                    "price": str(o.price),
                    "remaining_quantity": str(remaining),
                    "asset": asset,
                    "locked": str(amount),
                })
                locked_totals[asset] = locked_totals.get(asset, ZERO) + amount
            locks.sort(key=lambda e: e["order_id"])

            assets = {a for (acc, a) in self.state.balances if acc == account}
            assets |= {a for (acc, a) in self.state.frozen if acc == account}
            assets |= set(locked_totals)
            totals = {}
            for asset in sorted(assets):
                balance = self.state.balances.get((account, asset), ZERO)
                frozen = self.state.frozen.get((account, asset), ZERO)
                totals[asset] = {
                    "balance": str(balance),
                    "frozen": str(frozen),
                    "available": str(balance - frozen),
                    "locked": str(locked_totals.get(asset, ZERO)),
                }
            return {
                "account": account,
                "market": market,
                "locks": locks,
                "totals": totals,
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
