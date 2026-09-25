"""本地链上订单簿 DEX 撮合与结算服务的 HTTP 接口层。"""
from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from dex.engine import MatchingEngine
from dex.errors import DexError

app = FastAPI(title="onchain-orderbook-dex")

engine = MatchingEngine(
    data_path=os.environ.get("DEX_DATA_PATH", "dex_state.json"),
    admin_token=os.environ.get("DEX_ADMIN_TOKEN"),
)


@app.exception_handler(DexError)
async def dex_error_handler(_: Request, exc: DexError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status,
                        content={"error": {"code": exc.code, "message": exc.message}})


class DepositRequest(BaseModel):
    account_id: str
    asset: str
    amount: str = Field(..., description="十进制数量字符串")


class PlaceOrderRequest(BaseModel):
    account_id: str
    market: str
    side: str
    price: str
    quantity: str
    idempotency_key: str


class CancelOrderRequest(BaseModel):
    account_id: str


class RiskParamsRequest(BaseModel):
    market: str
    max_deviation_bps: int | None = None
    reference_ttl_seconds: int | None = None
    reference_price: str | None = None


@app.get("/")
def read_root():
    return {"service": "onchain-orderbook-dex", "status": "ok"}


@app.post("/accounts/deposit")
def deposit(req: DepositRequest):
    return engine.deposit(req.account_id, req.asset, req.amount)


@app.get("/accounts/{account_id}")
def get_account(account_id: str):
    return engine.get_account(account_id)


@app.post("/orders", status_code=201)
def place_order(req: PlaceOrderRequest):
    return engine.place_order(req.account_id, req.market, req.side,
                              req.price, req.quantity, req.idempotency_key)


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, req: CancelOrderRequest):
    return engine.cancel_order(req.account_id, order_id)


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    return engine.get_order(order_id)


@app.get("/markets/{market}/book")
def get_book(market: str, levels: int = 10):
    return engine.get_book(market, levels)


@app.get("/markets/{market}/trades")
def list_trades(market: str):
    return engine.list_trades(market)


@app.post("/admin/pause")
def pause(x_admin_token: str = Header(default="")):
    return engine.pause(x_admin_token)


@app.post("/admin/resume")
def resume(x_admin_token: str = Header(default="")):
    return engine.resume(x_admin_token)


@app.post("/admin/risk-params")
def set_risk_params(req: RiskParamsRequest, x_admin_token: str = Header(default="")):
    return engine.set_risk_params(
        x_admin_token, req.market,
        max_deviation_bps=req.max_deviation_bps,
        reference_ttl_seconds=req.reference_ttl_seconds,
        reference_price=req.reference_price)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
