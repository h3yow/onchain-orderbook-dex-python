"""风险控制：交易暂停/恢复、参考价有效性与偏离阈值检查。"""
from __future__ import annotations

import logging
from decimal import Decimal

from .errors import (
    InvalidPrice,
    MarketPaused,
    PriceDeviationExceeded,
    ReferencePriceMissing,
    ReferencePriceStale,
)
from .models import MarketConfig

logger = logging.getLogger("dex.risk")

BPS = Decimal("10000")


class RiskController:
    def __init__(self, markets: dict[str, MarketConfig]) -> None:
        self._markets = markets
        self._paused = False
        # market -> {"price": Decimal, "ts": float}
        self._reference: dict[str, dict] = {}
        # 运行时覆盖的风控参数
        self._overrides: dict[str, dict] = {}

    def pause(self) -> None:
        self._paused = True
        logger.warning("trading PAUSED")

    def resume(self) -> None:
        self._paused = False
        logger.warning("trading RESUMED")

    @property
    def paused(self) -> bool:
        return self._paused

    def set_reference_price(self, market: str, price: Decimal, ts: float) -> None:
        if price <= 0:
            raise InvalidPrice(f"reference price must be positive, got {price}")
        self._reference[market] = {"price": price, "ts": ts}
        logger.info("reference price market=%s price=%s ts=%s", market, price, ts)

    def set_params(self, market: str, max_deviation_bps: int | None,
                   reference_ttl_seconds: int | None) -> None:
        ov = self._overrides.setdefault(market, {})
        if max_deviation_bps is not None:
            ov["max_deviation_bps"] = max_deviation_bps
        if reference_ttl_seconds is not None:
            ov["reference_ttl_seconds"] = reference_ttl_seconds
        logger.info("risk params market=%s overrides=%s", market, ov)

    def _param(self, market: str, name: str):
        ov = self._overrides.get(market, {})
        if name in ov:
            return ov[name]
        return getattr(self._markets[market], name)

    def check_can_match(self, market: str, price: Decimal, now: float) -> None:
        """撮合前置检查：暂停、参考价缺失/过期/偏离阈值。"""
        if self._paused:
            raise MarketPaused("trading is paused; matching is disabled")
        ref = self._reference.get(market)
        if ref is None:
            raise ReferencePriceMissing(f"no reference price for market {market}")
        ttl = self._param(market, "reference_ttl_seconds")
        age = now - ref["ts"]
        if age > ttl:
            raise ReferencePriceStale(
                f"reference price for {market} is stale: age={age:.1f}s > ttl={ttl}s")
        max_dev_bps = Decimal(self._param(market, "max_deviation_bps"))
        deviation = abs(price - ref["price"]) / ref["price"] * BPS
        if deviation > max_dev_bps:
            raise PriceDeviationExceeded(
                f"price {price} deviates {deviation:.1f}bps from reference "
                f"{ref['price']} (max {max_dev_bps}bps)")
        logger.info(
            "risk check ok market=%s price=%s ref=%s deviation=%.1fbps ttl_age=%.1fs",
            market, price, ref["price"], deviation, age)

    def snapshot(self) -> dict:
        return {
            "paused": self._paused,
            "reference": {
                m: {"price": str(r["price"]), "ts": r["ts"]}
                for m, r in self._reference.items()
            },
            "overrides": self._overrides,
        }

    def restore(self, data: dict) -> None:
        self._paused = data.get("paused", False)
        self._reference = {
            m: {"price": Decimal(r["price"]), "ts": r["ts"]}
            for m, r in data.get("reference", {}).items()
        }
        self._overrides = dict(data.get("overrides", {}))
