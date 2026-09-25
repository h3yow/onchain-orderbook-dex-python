"""Shared pytest fixtures and helpers."""
from __future__ import annotations

import logging
import os
from decimal import Decimal

import pytest

from app.service import DexService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


@pytest.fixture
def now_holder():
    """Controllable clock so staleness can be tested deterministically."""
    class H:
        t = 1_000_000  # fixed base ms

        def __call__(self):
            return self.t

        def advance(self, ms):
            self.t += ms

    return H()


@pytest.fixture
def service(tmp_path, now_holder):
    path = str(tmp_path / "ledger.json")
    svc = DexService(ledger_path=path, clock=now_holder, admin_token="secret")
    svc.credit("alice", "USDC", Decimal("100000"))
    svc.credit("bob", "USDC", Decimal("100000"))
    svc.credit("bob", "BTC", Decimal("10"))
    svc.credit("carol", "USDC", Decimal("100000"))
    svc.credit("carol", "BTC", Decimal("10"))
    svc.set_reference_price("BTC-USDC", Decimal("100"))
    # isolated market used by tests that must not see other tests' orders
    from app.models import MarketConfig

    svc.register_market(MarketConfig(symbol="TST-USDC", base_asset="TST",
                                     quote_asset="USDC"))
    svc.set_reference_price("TST-USDC", Decimal("100"))
    return svc


def total_assets(svc):
    """Total owned quantity per asset.

    ``balance`` already INCLUDES the reserved ``frozen`` part
    (available = balance - frozen), so conservation is measured on
    balances alone. Freezing moves nothing out of balances; settlement
    moves balances between the two accounts.
    """
    totals: dict[str, Decimal] = {}
    for (_, asset), v in svc.state.balances.items():
        totals[asset] = totals.get(asset, Decimal(0)) + v
    return totals


def assert_conservation(svc, baseline):
    totals = total_assets(svc)
    assert totals == baseline, f"assets not conserved: {totals} != {baseline}"


def no_negative(svc):
    assert all(v >= 0 for v in svc.state.balances.values()), "negative balance"
    assert all(v >= 0 for v in svc.state.frozen.values()), "negative frozen"
