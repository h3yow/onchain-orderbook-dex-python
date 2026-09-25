"""本地账本：模拟链上资产账户，维护可用/冻结余额并保证原子结算。

不变量：
- 任意账户任意资产 available >= 0 且 frozen >= 0；
- 每种资产 (available + frozen) 的总量在撮合结算中守恒（deposit 是唯一的增发入口）。
"""
from __future__ import annotations

import logging
from decimal import Decimal

from .errors import InsufficientBalance, InsufficientFrozen, InvalidQuantity

logger = logging.getLogger("dex.ledger")

ZERO = Decimal("0")


class Ledger:
    def __init__(self) -> None:
        # balances[account][asset] = {"available": Decimal, "frozen": Decimal}
        self._balances: dict[str, dict[str, dict[str, Decimal]]] = {}

    def _entry(self, account_id: str, asset: str) -> dict[str, Decimal]:
        return self._balances.setdefault(account_id, {}).setdefault(
            asset, {"available": ZERO, "frozen": ZERO}
        )

    @staticmethod
    def _check_amount(amount: Decimal) -> None:
        if amount <= ZERO:
            raise InvalidQuantity(f"amount must be positive, got {amount}")

    def credit(self, account_id: str, asset: str, amount: Decimal) -> None:
        """充值（本地模拟链上入账），是资产总量的唯一入口。"""
        self._check_amount(amount)
        entry = self._entry(account_id, asset)
        entry["available"] += amount
        logger.info("credit account=%s asset=%s amount=%s -> available=%s",
                    account_id, asset, amount, entry["available"])

    def freeze(self, account_id: str, asset: str, amount: Decimal) -> None:
        """可用 -> 冻结。余额不足时拒绝。"""
        self._check_amount(amount)
        entry = self._entry(account_id, asset)
        if entry["available"] < amount:
            raise InsufficientBalance(
                f"account={account_id} asset={asset} available={entry['available']} "
                f"required={amount}")
        entry["available"] -= amount
        entry["frozen"] += amount
        logger.info("freeze account=%s asset=%s amount=%s -> available=%s frozen=%s",
                    account_id, asset, amount, entry["available"], entry["frozen"])

    def unfreeze(self, account_id: str, asset: str, amount: Decimal) -> None:
        """冻结 -> 可用。冻结额度不足时拒绝。"""
        self._check_amount(amount)
        entry = self._entry(account_id, asset)
        if entry["frozen"] < amount:
            raise InsufficientFrozen(
                f"account={account_id} asset={asset} frozen={entry['frozen']} "
                f"required={amount}")
        entry["frozen"] -= amount
        entry["available"] += amount
        logger.info("unfreeze account=%s asset=%s amount=%s -> available=%s frozen=%s",
                    account_id, asset, amount, entry["available"], entry["frozen"])

    def settle_trade(self, buyer_id: str, seller_id: str, base: str, quote: str,
                     price: Decimal, quantity: Decimal,
                     buyer_frozen_price: Decimal) -> None:
        """原子结算一笔成交。

        - 买方：冻结的 quote 按 *其下单价* 冻结，按成交价（maker 价）扣除，
          价差退回可用；获得 base。
        - 卖方：冻结的 base 扣除，获得 quote。
        任一前置条件不满足则整体不生效（先校验后修改）。
        """
        cost = price * quantity
        frozen_cost = buyer_frozen_price * quantity
        buyer_quote = self._entry(buyer_id, quote)
        seller_base = self._entry(seller_id, base)
        # 先校验，保证原子性
        if buyer_quote["frozen"] < cost:
            raise InsufficientFrozen(
                f"buyer={buyer_id} quote frozen={buyer_quote['frozen']} < cost={cost}")
        if seller_base["frozen"] < quantity:
            raise InsufficientFrozen(
                f"seller={seller_id} base frozen={seller_base['frozen']} < qty={quantity}")
        # 再修改：买方扣冻结 quote、补 base；卖方扣冻结 base、补 quote
        buyer_quote["frozen"] -= cost
        if frozen_cost > cost:
            # 买方下单价高于成交价，价差解冻退回
            buyer_quote["frozen"] -= (frozen_cost - cost)
            buyer_quote["available"] += (frozen_cost - cost)
        self._entry(buyer_id, base)["available"] += quantity
        seller_base["frozen"] -= quantity
        self._entry(seller_id, quote)["available"] += cost
        logger.info(
            "settle buyer=%s seller=%s base=%s quote=%s price=%s qty=%s cost=%s",
            buyer_id, seller_id, base, quote, price, quantity, cost)

    def available(self, account_id: str, asset: str) -> Decimal:
        return self._entry(account_id, asset)["available"]

    def frozen(self, account_id: str, asset: str) -> Decimal:
        return self._entry(account_id, asset)["frozen"]

    def assert_conservation(self) -> None:
        """校验不变量：无负余额（总量守恒由冻结/结算的对称性保证）。"""
        for account, assets in self._balances.items():
            for asset, entry in assets.items():
                assert entry["available"] >= ZERO, (
                    f"negative available: {account} {asset} {entry['available']}")
                assert entry["frozen"] >= ZERO, (
                    f"negative frozen: {account} {asset} {entry['frozen']}")

    def totals(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for assets in self._balances.values():
            for asset, entry in assets.items():
                totals[asset] = totals.get(asset, ZERO) + entry["available"] + entry["frozen"]
        return totals

    def snapshot(self) -> dict:
        return {
            account: {
                asset: {"available": str(e["available"]), "frozen": str(e["frozen"])}
                for asset, e in assets.items()
            }
            for account, assets in self._balances.items()
        }

    def restore(self, data: dict) -> None:
        self._balances = {
            account: {
                asset: {"available": Decimal(e["available"]), "frozen": Decimal(e["frozen"])}
                for asset, e in assets.items()
            }
            for account, assets in data.items()
        }
