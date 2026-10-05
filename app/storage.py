"""JSON-file based durable ledger storage.

All Decimal values are serialised as strings so precision is preserved.
Writes are atomic (temp file + os.replace) so a crash never leaves a
half-written ledger.
"""
from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal

from app.models import (
    Fill,
    LedgerState,
    MarketConfig,
    Order,
    OrderStatus,
    ReferencePrice,
    RiskParams,
    Side,
)


def _side(side: Side) -> str:
    return side.value


def _order_to_json(o: Order) -> dict:
    return {
        "order_id": o.order_id,
        "account": o.account,
        "market": o.market,
        "side": o.side.value,
        "price": str(o.price),
        "quantity": str(o.quantity),
        "filled_qty": str(o.filled_qty),
        "status": o.status.value,
        "created_at_ms": o.created_at_ms,
        "sequence": o.sequence,
    }


def _fill_to_json(f: Fill) -> dict:
    return {
        "fill_id": f.fill_id,
        "market": f.market,
        "buy_order_id": f.buy_order_id,
        "sell_order_id": f.sell_order_id,
        "taker_order_id": f.taker_order_id,
        "maker_order_id": f.maker_order_id,
        "buyer": f.buyer,
        "seller": f.seller,
        "price": str(f.price),
        "quantity": str(f.quantity),
        "at_ms": f.at_ms,
        "sequence": f.sequence,
    }


def encode_state(state: LedgerState) -> str:
    data = {
        "balances": {f"{a}|{asset}": str(v) for (a, asset), v in state.balances.items()},
        "frozen": {f"{a}|{asset}": str(v) for (a, asset), v in state.frozen.items()},
        "credited": {asset: str(v) for asset, v in state.credited.items()},
        "orders": {oid: _order_to_json(o) for oid, o in state.orders.items()},
        "fills": [_fill_to_json(f) for f in state.fills],
        "markets": {
            s: {
                "symbol": m.symbol,
                "base_asset": m.base_asset,
                "quote_asset": m.quote_asset,
                "tick_size": str(m.tick_size),
                "lot_size": str(m.lot_size),
            }
            for s, m in state.markets.items()
        },
        "references": {
            s: {"price": str(r.price), "at_ms": r.at_ms}
            for s, r in state.references.items()
        },
        "risk": {
            s: {
                "max_deviation_bps": r.max_deviation_bps,
                "reference_max_age_ms": r.reference_max_age_ms,
            }
            for s, r in state.risk.items()
        },
        "halted": dict(state.halted),
        "idempotency": [
            {"key": k, "kind": kind, "payload": payload}
            for k, (kind, payload) in state.idempotency.items()
        ],
        "order_sequence": state.order_sequence,
        "fill_sequence": state.fill_sequence,
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def _order_from_json(d: dict) -> Order:
    return Order(
        order_id=d["order_id"],
        account=d["account"],
        market=d["market"],
        side=Side(d["side"]),
        price=Decimal(d["price"]),
        quantity=Decimal(d["quantity"]),
        filled_qty=Decimal(d["filled_qty"]),
        status=OrderStatus(d["status"]),
        created_at_ms=d["created_at_ms"],
        sequence=d["sequence"],
    )


def _fill_from_json(d: dict) -> Fill:
    return Fill(
        fill_id=d["fill_id"],
        market=d["market"],
        buy_order_id=d["buy_order_id"],
        sell_order_id=d["sell_order_id"],
        taker_order_id=d["taker_order_id"],
        maker_order_id=d["maker_order_id"],
        buyer=d["buyer"],
        seller=d["seller"],
        price=Decimal(d["price"]),
        quantity=Decimal(d["quantity"]),
        at_ms=d["at_ms"],
        sequence=d["sequence"],
    )


def decode_state(raw: str) -> LedgerState:
    data = json.loads(raw)
    state = LedgerState()
    for k, v in data.get("balances", {}).items():
        account, asset = k.split("|", 1)
        state.balances[(account, asset)] = Decimal(v)
    for k, v in data.get("frozen", {}).items():
        account, asset = k.split("|", 1)
        state.frozen[(account, asset)] = Decimal(v)
    state.credited = {
        asset: Decimal(v) for asset, v in data.get("credited", {}).items()
    }
    if not state.credited and state.balances:
        # legacy ledger without credit tracking: assume it was consistent
        # at save time and derive the conservation baseline from balances
        for (_, asset), v in state.balances.items():
            state.credited[asset] = state.credited.get(asset, Decimal(0)) + v
    state.orders = {oid: _order_from_json(d) for oid, d in data.get("orders", {}).items()}
    state.fills = [_fill_from_json(d) for d in data.get("fills", [])]
    state.markets = {
        s: MarketConfig(
            symbol=m["symbol"],
            base_asset=m["base_asset"],
            quote_asset=m["quote_asset"],
            tick_size=Decimal(m["tick_size"]),
            lot_size=Decimal(m["lot_size"]),
        )
        for s, m in data.get("markets", {}).items()
    }
    state.references = {
        s: ReferencePrice(price=Decimal(r["price"]), at_ms=r["at_ms"])
        for s, r in data.get("references", {}).items()
    }
    state.risk = {
        s: RiskParams(
            max_deviation_bps=r["max_deviation_bps"],
            reference_max_age_ms=r["reference_max_age_ms"],
        )
        for s, r in data.get("risk", {}).items()
    }
    state.halted = dict(data.get("halted", {}))
    state.idempotency = {
        item["key"]: (item["kind"], item["payload"]) for item in data.get("idempotency", [])
    }
    state.order_sequence = data.get("order_sequence", 0)
    state.fill_sequence = data.get("fill_sequence", 0)
    return state


def save_state(path: str, state: LedgerState) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encode_state(state))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(path: str) -> LedgerState | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    if not raw.strip():
        return None
    return decode_state(raw)
