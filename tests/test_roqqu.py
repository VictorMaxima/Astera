"""Checks the Roqqu wiring against the examples in Roqqu's docs, with no network access."""
import json
import os

os.environ.setdefault("ASTERA_MOCK", "1")
os.environ.setdefault("ASTERA_EXTERNAL_CHECKS", "0")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_astera.db")

import httpx
import pytest

from app import roqqu as r
from app.config import settings

CUSTOMER = {"email": "jean@email.com", "first_name": "Jean", "last_name": "Billy"}


def make_client(monkeypatch, handler):
    monkeypatch.setattr(settings, "mock", False)
    monkeypatch.setattr(settings, "roqqu_mode", "test")
    monkeypatch.setattr(settings, "roqqu_api_key", "key123")
    c = r.RoqquClient()
    c.http = httpx.Client(base_url=r.BASE_URL, headers=c.http.headers, transport=httpx.MockTransport(handler))
    return c


def test_generate_wallet_matches_docs(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["path"], seen["headers"], seen["body"] = req.url.path, req.headers, json.loads(req.content)
        return httpx.Response(200, json={"status": "success", "message": "wallet generated successfully",
                                         "data": {"address": "0xabc0000000000000000000000000000000000001"}})

    addr = make_client(monkeypatch, handler).create_deposit_address("inv1", "bep20", "usdt", CUSTOMER)
    assert addr == "0xabc0000000000000000000000000000000000001"
    assert seen["path"] == "/v1/wallets/generate-wallet"
    assert seen["headers"]["x-staging-api-key"] == "key123"          # test mode header
    assert "x-api-key" not in seen["headers"]
    assert seen["headers"]["content-type"].startswith("application/json")
    assert seen["body"]["network"] == "bep20"
    assert json.loads(seen["body"]["customer"]) == CUSTOMER            # customer sent as a JSON string, per docs


def test_balances_is_form_encoded(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["ctype"], seen["body"] = req.headers["content-type"], req.content.decode()
        return httpx.Response(200, json={"status": "success", "data": {"balance": {"usdt": 137, "btc": 0}}})

    bal = make_client(monkeypatch, handler).get_balance("ebentest@yopmail.com", "usdt")
    assert bal["usdt"] == 137
    assert seen["ctype"].startswith("application/x-www-form-urlencoded")
    assert "email=ebentest%40yopmail.com" in seen["body"] and "token=usdt" in seen["body"]


def test_roqqu_fail_status_raises_with_message(monkeypatch):
    handler = lambda req: httpx.Response(200, json={"status": "fail", "message": "unsupported network"})
    with pytest.raises(RuntimeError, match="unsupported network"):
        make_client(monkeypatch, handler).create_deposit_address("inv1", "dogecoin", "doge", CUSTOMER)


def test_send_matches_docs(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["path"], seen["ctype"], seen["body"] = req.url.path, req.headers["content-type"], req.content.decode()
        return httpx.Response(200, json={"status": "success", "message": "send operation successful",
                                         "data": {"refid": "10704380386654367514"}})

    out = make_client(monkeypatch, handler).send(address="TUyR2sEjzDZNmbnac2ZkJ54h64m6TmVN71",
                                                 amount="2", token="usdt", network="trc20")
    assert out == {"ref": "10704380386654367514", "tx_hash": None}   # hash arrives later
    assert seen["path"] == "/v1/wallets/send"
    assert seen["ctype"].startswith("application/x-www-form-urlencoded")
    for part in ("amount=2", "network=trc20", "to=TUyR2sEjzDZNmbnac2ZkJ54h64m6TmVN71", "token=usdt"):
        assert part in seen["body"]


def test_send_error_message_surfaces(monkeypatch):
    handler = lambda req: httpx.Response(200, json={"status": "fail", "message": "insufficient USDT balance"})
    with pytest.raises(RuntimeError, match="insufficient USDT balance"):
        make_client(monkeypatch, handler).send(address="0x1", amount="1", token="usdt", network="bep20")


def test_list_deposits_uses_get(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["method"], seen["path"] = req.method, req.url.path
        return httpx.Response(200, json={"status": "success", "data": [{"address": "0xa", "status": 1}]})

    assert make_client(monkeypatch, handler).list_deposits() == [{"address": "0xa", "status": 1}]
    assert (seen["method"], seen["path"]) == ("GET", "/v1/transaction/token-deposit")


def test_reconcile_credits_deposit_once_and_fills_payout_hash(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import SessionLocal

    with TestClient(main.app) as client:
        inv = client.post("/invoices", json={
            "freelancer_email": "f@x.com", "freelancer_payout_address": "0x" + "1" * 40,
            "client_email": "c@x.com", "description": "Job", "amount": "150",
            "token": "usdt", "network": "bep20"}).json()

        deposit = {"address": inv["pay_to"].upper().replace("0X", "0x"), "status": 1, "value": 150,
                   "token": "usdt", "network": "bep20", "tx_hash": "0xdep1"}
        monkeypatch.setattr(main.settings, "mock", False)
        monkeypatch.setattr(main.roqqu, "list_deposits", lambda: [deposit, {**deposit, "status": 0, "tx_hash": "0xpending"}])

        with SessionLocal() as db:
            assert main.reconcile(db)["deposits"] == 1
            assert main.reconcile(db)["deposits"] == 0   # polling again must not double-credit
        F, C = {"X-Astera-Key": inv["freelancer_key"]}, {"X-Astera-Key": inv["client_key"]}
        got = client.get(f"/invoices/{inv['id']}", headers=C).json()
        assert got["status"] == "funded" and got["funded_amount"] == "150"

        # payout hash is filled in from the withdrawals list once Roqqu has one
        client.post(f"/invoices/{inv['id']}/deliver", headers=F)
        monkeypatch.setattr(main.roqqu, "send", lambda **kw: {"ref": "R1", "tx_hash": None})
        assert client.post(f"/invoices/{inv['id']}/release", headers=C).json()["release_ref"] == "R1"
        monkeypatch.setattr(main.roqqu, "list_withdrawals", lambda: [{"refid": "R1", "status": 3, "tx_hash": "0xchain"}])
        with SessionLocal() as db:
            assert main.reconcile(db)["payouts"] == 1
        assert client.get(f"/invoices/{inv['id']}", headers=C).json()["release_tx_hash"] == "0xchain"
