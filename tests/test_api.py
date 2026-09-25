"""HTTP layer: endpoints, admin auth, error envelope."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.service import DexService


@pytest.fixture
def client(tmp_path, now_holder):
    svc = DexService(ledger_path=str(tmp_path / "ledger.json"),
                     clock=now_holder, admin_token="secret")
    return TestClient(create_app(svc)), svc


def test_full_trade_flow_over_http(client):
    c, svc = client
    h = {"X-Admin-Token": "secret"}
    assert c.post("/admin/credit", headers=h,
                  json={"account": "a", "asset": "USDC", "amount": "10000"}).status_code == 200
    assert c.post("/admin/credit", headers=h,
                  json={"account": "b", "asset": "BTC", "amount": "5"}).status_code == 200
    assert c.post("/admin/reference-price", headers=h,
                  json={"market": "BTC-USDC", "price": "100"}).status_code == 200

    r = c.post("/orders", json={"market": "BTC-USDC", "account": "b",
                                "side": "SELL", "price": "100", "quantity": "1"})
    assert r.status_code == 200
    sell_id = r.json()["order_id"]

    r = c.post("/orders", json={"market": "BTC-USDC", "account": "a",
                                "side": "BUY", "price": "100", "quantity": "0.5",
                                "idempotency_key": "http-1"})
    body = r.json()
    assert body["status"] == "FILLED" and len(body["fills"]) == 1

    # replay
    body2 = c.post("/orders", json={"market": "BTC-USDC", "account": "a",
                                   "side": "BUY", "price": "100", "quantity": "0.5",
                                   "idempotency_key": "http-1"}).json()
    assert body2["order_id"] == body["order_id"]
    print("ASSERT http flow + idempotent replay ok")

    g = c.get(f"/accounts/a/balances/BTC").json()
    assert g["balance"] == "0.5"

    snap = c.get("/markets", params={"market": "BTC-USDC"}).json()
    assert snap["markets"]["BTC-USDC"]["asks"] == [{"price": "100", "quantity": "0.5"}]

    rc = c.post("/orders/cancel", json={"order_id": sell_id, "account": "b"}).json()
    assert rc["status"] == "CANCELLED"


def test_error_envelope_is_distinguishable(client):
    c, _ = client
    r = c.post("/orders", json={"market": "BTC-USDC", "account": "a",
                                "side": "BUY", "price": "0", "quantity": "1"})
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_PRICE"
    r = c.post("/orders", json={"market": "BTC-USDC", "account": "a",
                                "side": "BUY", "price": "100", "quantity": "0"})
    assert r.json()["error"] == "INVALID_QUANTITY"
    r = c.post("/orders", json={"market": "BTC-USDC", "account": "a",
                                "side": "BUY", "price": "100", "quantity": "1"})
    assert r.json()["error"] == "INSUFFICIENT_BALANCE"
    r = c.post("/orders", json={"market": "NOPE", "account": "a",
                                "side": "BUY", "price": "100", "quantity": "1"})
    assert r.json()["error"] == "UNKNOWN_MARKET"
    print("ASSERT each rejection carries its own error code")


def test_admin_endpoints_require_token(client):
    c, _ = client
    r = c.post("/admin/halt", json={"market": "BTC-USDC", "halted": True})
    assert r.status_code == 400 and r.json()["error"] == "UNAUTHORIZED"
    r = c.post("/admin/halt", headers={"X-Admin-Token": "wrong"},
               json={"market": "BTC-USDC", "halted": True})
    assert r.json()["error"] == "UNAUTHORIZED"
    r = c.post("/admin/halt", headers={"X-Admin-Token": "secret"},
               json={"market": "BTC-USDC", "halted": True})
    assert r.status_code == 200 and r.json()["halted"] is True
    print("ASSERT admin auth enforced and valid token works")


def test_halted_market_blocks_over_http(client):
    c, _ = client
    h = {"X-Admin-Token": "secret"}
    c.post("/admin/credit", headers=h,
           json={"account": "a", "asset": "USDC", "amount": "10000"})
    c.post("/admin/credit", headers=h,
           json={"account": "b", "asset": "BTC", "amount": "5"})
    c.post("/admin/reference-price", headers=h,
           json={"market": "BTC-USDC", "price": "100"})
    c.post("/admin/halt", headers=h,
           json={"market": "BTC-USDC", "halted": True})
    c.post("/orders", json={"market": "BTC-USDC", "account": "b",
                            "side": "SELL", "price": "100", "quantity": "1"})
    r = c.post("/orders", json={"market": "BTC-USDC", "account": "a",
                                "side": "BUY", "price": "100", "quantity": "1"})
    body = r.json()
    assert body["fills"] == [] and body["blocked_reason"] == "MARKET_HALTED"
    print("ASSERT halt exposed in order response over HTTP")
