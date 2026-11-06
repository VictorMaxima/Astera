import os

# Must be set before the app imports its settings.
os.environ["ASTERA_MOCK"] = "1"
os.environ["ASTERA_EXTERNAL_CHECKS"] = "0"
os.environ["DATABASE_URL"] = "sqlite:///./test_astera.db"
os.environ["ANTHROPIC_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient

from app.main import app

PAYOUT = "0x" + "1" * 40


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def new_invoice(client, **overrides):
    body = {
        "freelancer_email": "free@example.com",
        "freelancer_payout_address": PAYOUT,
        "client_email": "client@example.com",
        "description": "Logo design",
        "amount": "150",
        "token": "usdt",
        "network": "bep20",
    }
    body.update(overrides)
    return client.post("/invoices", json=body)


def key(k):
    return {"X-Astera-Key": k}


@pytest.fixture()
def inv(client):
    return new_invoice(client).json()


def test_full_flow_with_roles(client, inv):
    i, F, C = inv["id"], key(inv["freelancer_key"]), key(inv["client_key"])
    assert inv["status"] == "awaiting_payment"

    paid = client.post(f"/dev/simulate-payment/{i}", headers=C).json()
    assert paid["status"] == "funded"

    assert client.post(f"/invoices/{i}/deliver", headers=F).json()["status"] == "delivered"

    check = client.get(f"/invoices/{i}/release-check", headers=C).json()
    assert check["verdict"] == "safe"

    done = client.post(f"/invoices/{i}/release", headers=C).json()
    assert done["status"] == "released"
    assert done["release_tx_hash"]


def test_requests_without_a_valid_key_are_rejected(client, inv):
    i = inv["id"]
    for method, path in [("get", f"/invoices/{i}"), ("get", f"/invoices/{i}/events"),
                         ("post", f"/invoices/{i}/deliver"), ("post", f"/invoices/{i}/release"),
                         ("get", f"/invoices/{i}/release-check"), ("post", f"/invoices/{i}/dispute")]:
        assert getattr(client, method)(path).status_code == 401, path
        assert getattr(client, method)(path, headers=key("wrong-key")).status_code == 401, path


def test_each_role_can_only_do_its_own_actions(client, inv):
    i, F, C = inv["id"], key(inv["freelancer_key"]), key(inv["client_key"])
    client.post(f"/dev/simulate-payment/{i}", headers=C)
    assert client.post(f"/invoices/{i}/deliver", headers=C).status_code == 403   # client can't mark delivered
    client.post(f"/invoices/{i}/deliver", headers=F)
    assert client.post(f"/invoices/{i}/release", headers=F).status_code == 403   # freelancer can't release
    assert client.get(f"/invoices/{i}/release-check", headers=F).status_code == 403
    assert client.get(f"/invoices/{i}", headers=F).status_code == 200            # both can view
    assert client.get(f"/invoices/{i}", headers=C).status_code == 200


def test_keys_are_not_leaked_or_stored_in_plaintext(client, inv):
    i, C = inv["id"], key(inv["client_key"])
    body = client.get(f"/invoices/{i}", headers=C).json()
    assert "client_key" not in body and "freelancer_key" not in body
    from app.db import Invoice, SessionLocal
    with SessionLocal() as db:
        row = db.get(Invoice, i)
        assert inv["client_key"] not in (row.client_key_hash, row.freelancer_key_hash)
        assert row.client_key_hash != row.freelancer_key_hash


def test_one_invoices_key_does_not_open_another(client, inv):
    other = new_invoice(client).json()
    assert client.get(f"/invoices/{other['id']}", headers=key(inv["client_key"])).status_code == 401


def test_either_party_can_dispute(client, inv):
    i = inv["id"]
    client.post(f"/dev/simulate-payment/{i}", headers=key(inv["client_key"]))
    assert client.post(f"/invoices/{i}/dispute", headers=key(inv["freelancer_key"])).json()["status"] == "disputed"


def test_partial_payment_stays_awaiting(client, inv):
    r = client.post(f"/dev/simulate-payment/{inv['id']}?amount=50", headers=key(inv["client_key"])).json()
    assert r["status"] == "awaiting_payment"
    assert r["funded_amount"].startswith("50")


def test_cannot_release_before_delivery(client, inv):
    C = key(inv["client_key"])
    client.post(f"/dev/simulate-payment/{inv['id']}", headers=C)
    assert client.post(f"/invoices/{inv['id']}/release", headers=C).status_code == 409


def test_rejects_bad_payout_address_and_network_mismatch(client):
    assert new_invoice(client, freelancer_payout_address="not-an-address").status_code == 422
    # TRC20 address on a BEP20 invoice
    assert new_invoice(client, freelancer_payout_address="T" + "A" * 33).status_code == 422
    assert new_invoice(client, token="btc", network="bitcoin").status_code == 422


def test_duplicate_webhook_not_double_credited(client, inv):
    payload = {"address": inv["pay_to"], "amount": "150", "token": "usdt",
               "network": "bep20", "tx_hash": "0xabc"}
    assert client.post("/webhooks/roqqu", json=payload).json()["result"] == "ok"
    assert client.post("/webhooks/roqqu", json=payload).json()["result"] == "duplicate"
    got = client.get(f"/invoices/{inv['id']}", headers=key(inv["client_key"])).json()
    assert got["funded_amount"].startswith("150")
