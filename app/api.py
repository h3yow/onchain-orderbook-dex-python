"""FastAPI HTTP layer.

Admin endpoints require header ``X-Admin-Token`` (default: ``admin-token``).
"""
from __future__ import annotations

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

from app.errors import DexError
from app.models import (
    BatchCancelRequest,
    CancelOrderRequest,
    CreditRequest,
    HaltRequest,
    PlaceOrderRequest,
    ReferencePriceRequest,
    RiskParams,
    RiskParamsRequest,
)


def create_app(service) -> FastAPI:
    app = FastAPI(title="Local On-Chain Order Book DEX", version="0.1.0")

    @app.exception_handler(DexError)
    async def _dex_error_handler(request, exc: DexError):
        return JSONResponse(status_code=400, content=exc.to_dict())

    # ------------------------------------------------------------- trading

    @app.post("/orders")
    def place_order(req: PlaceOrderRequest):
        return service.place_order(req)

    @app.post("/orders/cancel")
    def cancel_order(req: CancelOrderRequest):
        return service.cancel_order(req)

    @app.post("/orders/batch-cancel")
    def batch_cancel(req: BatchCancelRequest):
        return service.batch_cancel(req)

    @app.get("/orders/{order_id}")
    def get_order(order_id: str):
        return service.get_order(order_id)

    @app.get("/accounts/{account}/balances/{asset}")
    def get_balance(account: str, asset: str):
        return service.get_balance(account, asset)

    @app.get("/accounts/{account}/locks")
    def get_locks(account: str, market: str | None = None):
        return service.get_locks(account, market)

    @app.get("/markets")
    def get_snapshot(market: str | None = None):
        return service.get_snapshot(market)

    @app.get("/")
    def root():
        return {"service": "local-orderbook-dex", "status": "ok"}

    # --------------------------------------------------------------- admin

    @app.post("/admin/credit")
    def admin_credit(req: CreditRequest, x_admin_token: str = Header(default="")):
        service.require_admin(x_admin_token)
        service.credit(req.account, req.asset, req.amount)
        return {"status": "ok"}

    @app.post("/admin/reference-price")
    def admin_reference(req: ReferencePriceRequest,
                        x_admin_token: str = Header(default="")):
        service.require_admin(x_admin_token)
        service.set_reference_price(req.market, req.price)
        return {"status": "ok"}

    @app.post("/admin/risk-params")
    def admin_risk(req: RiskParamsRequest, x_admin_token: str = Header(default="")):
        service.require_admin(x_admin_token)
        service.set_risk_params(
            req.market,
            RiskParams(max_deviation_bps=req.max_deviation_bps,
                       reference_max_age_ms=req.reference_max_age_ms),
        )
        return {"status": "ok"}

    @app.post("/admin/halt")
    def admin_halt(req: HaltRequest, x_admin_token: str = Header(default="")):
        service.require_admin(x_admin_token)
        service.set_halted(req.market, req.halted)
        return {"status": "ok", "market": req.market, "halted": req.halted}

    @app.get("/admin/reconcile")
    def admin_reconcile(x_admin_token: str = Header(default="")):
        service.require_admin(x_admin_token)
        return service.reconcile()

    return app
