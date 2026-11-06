"""Roqqu API Services client.

STATUS OF EACH CALL
-------------------
  balances         DONE         POST /balances                  (form-encoded)
  create_wallet    DONE         POST /wallets/generate-wallet   (JSON)
  send             DONE         POST /wallets/send              (form-encoded, queues a payout, returns refid)
  deposits         DONE         GET  /transaction/token-deposit (Astera polls this to detect payments)
  withdrawals      DONE         GET  /transaction/token-withdrawal (Astera polls this for payout tx hashes)
  webhook          NOT USED     the docs seen so far cover the payment-link product, with an encrypted
                                body and no decryption steps; the placeholders below stay failing closed

HOW TO GO FROM DEMO TO REAL
---------------------------
1. Fill in the PLACEHOLDER items below once you have the send and webhook docs.
2. In .env set ASTERA_MOCK=0, ROQQU_API_KEY=<your test key>, ROQQU_MODE=test.
3. Try each call by hand in test mode first, e.g.
   python -c "from app.roqqu import roqqu; print(roqqu.get_balance('you@example.com'))"

With ASTERA_MOCK=1 (default) no Roqqu call is made at all and fake data is returned.
"""
import hashlib
import hmac
import json
import uuid

import httpx

from .config import settings

BASE_URL = "https://service.roqqu.com/v1"  # PLACEHOLDER: confirm test mode uses the same base URL

# =====================================================================================
# ENDPOINT DETAILS. PLACEHOLDER items are marked; everything else comes from the Roqqu docs.
# =====================================================================================

ENDPOINTS = {
    "balances": "/balances",                    # documented
    "create_wallet": "/wallets/generate-wallet",  # documented
    "send": "/wallets/send",                    # documented
    "deposits": "/transaction/token-deposit",   # documented (GET)
    "withdrawals": "/transaction/token-withdrawal",  # documented (GET)
}

# How each endpoint wants its body: "form" (application/x-www-form-urlencoded) or "json".
ENCODING = {
    "balances": "form",       # documented
    "create_wallet": "json",  # documented
    "send": "form",           # documented
}


def build_create_wallet_request(network: str, customer: dict) -> dict:
    """Documented. `customer` needs email, first_name, last_name. The docs' example sends it as a JSON string."""
    return {"network": network, "customer": json.dumps(customer)}


def read_create_wallet_response(body: dict) -> str:
    """Documented: {"status": "success", "data": {"address": "..."}}"""
    return body["data"]["address"]


def build_send_request(*, address: str, amount: str, token: str, network: str) -> dict:
    """Documented. Note: the docs type `amount` as an integer; whether decimals are accepted is unconfirmed."""
    return {"amount": amount, "network": network, "to": address, "token": token}


def read_send_response(body: dict) -> dict:
    """Documented: the send is queued and returns {"data": {"refid": ...}}. The on-chain tx hash
    arrives later; Astera fills it in from GET /transaction/token-withdrawal."""
    return {"ref": str(body["data"]["refid"]), "tx_hash": None}


# Webhook: which header carries the signature, and which JSON fields hold each value.
SIGNATURE_HEADER = "x-roqqu-signature"  # PLACEHOLDER: real header name
WEBHOOK_FIELDS = {                      # PLACEHOLDER: left = Astera's name, right = Roqqu's field
    "address": "address",
    "amount": "amount",
    "token": "token",
    "network": "network",
    "tx_hash": "tx_hash",
}


def signature_matches(secret: str, raw_body: bytes, received: str) -> bool:
    """PLACEHOLDER: assumes HMAC-SHA256 hex of the raw body. Change to Roqqu's real scheme."""
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


# =====================================================================================
# Client. Normally you do not need to edit below this line.
# =====================================================================================

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _fake_address(network: str, seed: str) -> str:
    digest = hashlib.sha256(f"{network}:{seed}".encode()).digest()
    if network == "trc20":
        return "T" + "".join(B58[b % len(B58)] for b in digest[:33])
    return "0x" + digest.hex()[:40]


class RoqquClient:
    def __init__(self) -> None:
        # Test mode and live mode use different auth headers.
        header = "x-staging-api-key" if settings.roqqu_mode == "test" else "x-api-key"
        self.http = httpx.Client(base_url=BASE_URL, headers={header: settings.roqqu_api_key}, timeout=15)

    def _call(self, name: str, payload: dict | None = None, method: str = "POST") -> dict:
        path = ENDPOINTS.get(name)
        if not path:
            raise NotImplementedError(
                f"Roqqu endpoint '{name}' is not set. Fill in ENDPOINTS['{name}'] in app/roqqu.py "
                "from the Roqqu docs, or set ASTERA_MOCK=1."
            )
        if method == "GET":
            r = self.http.get(path)
        else:
            kwargs = {"json": payload} if ENCODING.get(name) == "json" else {"data": payload}
            r = self.http.post(path, **kwargs)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400 or body.get("status") != "success":
            raise RuntimeError(f"Roqqu {name} failed ({r.status_code}): {body.get('message', r.text[:200])}")
        return body

    def get_balance(self, email: str, token: str | None = None) -> dict:
        """POST /balances. Returns the flat per-token balance row."""
        if settings.mock:
            return {"usdt": 1000, "usdc": 1000}
        payload = {"email": email, **({"token": token} if token else {})}
        return self._call("balances", payload)["data"]["balance"]

    def create_deposit_address(self, invoice_id: str, network: str, token: str, customer: dict | None = None) -> str:
        """POST /wallets/generate-wallet. `customer` = {email, first_name, last_name}."""
        if settings.mock:
            return _fake_address(network, invoice_id)
        if not customer:
            raise ValueError("Roqqu needs customer details (email, first_name, last_name) to generate a wallet")
        body = self._call("create_wallet", build_create_wallet_request(network, customer))
        return read_create_wallet_response(body)

    def send(self, *, address: str, amount: str, token: str, network: str) -> dict:
        """POST /wallets/send. Returns {"ref": refid, "tx_hash": None}; the hash comes later."""
        if settings.mock:
            return {"ref": "mock" + uuid.uuid4().hex[:16], "tx_hash": "0xmock" + uuid.uuid4().hex}
        body = self._call("send", build_send_request(address=address, amount=amount,
                                                     token=token, network=network))
        return read_send_response(body)

    def list_deposits(self) -> list[dict]:
        """GET /transaction/token-deposit. Items have address, value, token, network, tx_hash, status (1 = credited)."""
        if settings.mock:
            return []
        return self._call("deposits", method="GET")["data"]

    def list_withdrawals(self) -> list[dict]:
        """GET /transaction/token-withdrawal. Items have refid, status, tx_hash. Status: 0 pending,
        1 processing, 2 failed, 3 completed, 5 refund queued."""
        if settings.mock:
            return []
        return self._call("withdrawals", method="GET")["data"]

    def verify_webhook(self, raw_body: bytes, headers) -> bool:
        """Fails closed: with no ROQQU_WEBHOOK_SECRET, real webhooks are rejected."""
        if settings.mock:
            return True
        if not settings.roqqu_webhook_secret:
            return False
        return signature_matches(settings.roqqu_webhook_secret, raw_body, headers.get(SIGNATURE_HEADER, ""))

    def parse_webhook(self, payload: dict) -> dict:
        return {ours: payload[theirs] for ours, theirs in WEBHOOK_FIELDS.items()}


roqqu = RoqquClient()
