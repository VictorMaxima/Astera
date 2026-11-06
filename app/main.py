import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import timedelta
from decimal import Decimal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import explain, safety
from .config import settings
from .db import Event, Invoice, SessionLocal, init_db, utcnow
from .roqqu import roqqu
from .state import InvalidTransition, Status, assert_transition


log_ = logging.getLogger("astera")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # In live mode, find deposits and payout hashes by polling Roqqu (no webhook needed).
    task = asyncio.create_task(poll_roqqu()) if not settings.mock else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Astera", description="Stablecoin invoicing with escrow and built-in transaction safety", lifespan=lifespan)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- helpers ----------

def fmt(value) -> str:
    """Decimal as a plain string without trailing zeros: 150.00000000 -> 150."""
    s = format(Decimal(str(value)), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def log(db: Session, inv: Invoice, kind: str, detail: str = "") -> None:
    db.add(Event(invoice_id=inv.id, kind=kind, detail=detail))


def transition(db: Session, inv: Invoice, new: Status) -> None:
    try:
        assert_transition(inv.status, new)
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    log(db, inv, "status_change", f"{inv.status} -> {new.value}")
    inv.status = new.value


def get_invoice(db: Session, invoice_id: str) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status == Status.AWAITING_PAYMENT.value and utcnow() > inv.expires_at:
        transition(db, inv, Status.EXPIRED)
        db.commit()
    return inv


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def role_of(inv: Invoice, key: str) -> str | None:
    """Which party an access key belongs to, or None. Keys are random secrets; only hashes are stored."""
    if not key:
        return None
    h = hash_key(key)
    if hmac.compare_digest(h, inv.client_key_hash):
        return "client"
    if hmac.compare_digest(h, inv.freelancer_key_hash):
        return "freelancer"
    return None


def authed_invoice(db: Session, invoice_id: str, request: Request, allow: set[str]) -> Invoice:
    inv = get_invoice(db, invoice_id)
    role = role_of(inv, request.headers.get("x-astera-key", ""))
    if role is None:
        raise HTTPException(401, "Missing or invalid access key")
    if role not in allow:
        raise HTTPException(403, "This action is only available to the " + " or ".join(sorted(allow)))
    return inv


def invoice_out(inv: Invoice) -> dict:
    return {
        "id": inv.id,
        "status": inv.status,
        "description": inv.description,
        "amount": fmt(inv.amount),
        "funded_amount": fmt(inv.funded_amount),
        "token": inv.token,
        "network": inv.network,
        "pay_to": inv.deposit_address,
        "warning": f"Send {inv.token.upper()} on {inv.network.upper()} only. Other assets or networks may be lost.",
        "freelancer_email": inv.freelancer_email,
        "client_email": inv.client_email,
        "expires_at": inv.expires_at.isoformat(),
        "release_tx_hash": inv.release_tx_hash,
        "release_ref": inv.release_ref,
    }


def apply_deposit(db: Session, inv: Invoice, *, amount, token: str, network: str, tx_hash: str) -> str:
    seen = inv.deposit_tx_hashes.split(",") if inv.deposit_tx_hashes else []
    if tx_hash in seen:
        return "duplicate"  # webhooks get retried; never credit twice

    if token.lower() != inv.token or network.lower() != inv.network:
        log(db, inv, "wrong_asset", f"Received {amount} {token}/{network}, expected {inv.token}/{inv.network}")
        inv.deposit_tx_hashes = ",".join(seen + [tx_hash])  # remember it so polling logs it only once
        db.commit()
        return "wrong_asset"

    inv.funded_amount = Decimal(inv.funded_amount) + Decimal(str(amount))
    inv.deposit_tx_hashes = ",".join(seen + [tx_hash])
    log(db, inv, "deposit", f"{amount} {token} tx={tx_hash}")

    if inv.status == Status.AWAITING_PAYMENT.value:
        if inv.funded_amount >= inv.amount:
            transition(db, inv, Status.FUNDED)
        else:
            log(db, inv, "partial_payment", f"{fmt(inv.funded_amount)}/{fmt(inv.amount)}")
    if inv.funded_amount > inv.amount:
        # Excess stays in the escrow wallet; a real product needs a refund flow for this.
        log(db, inv, "overpayment", f"{fmt(inv.funded_amount - inv.amount)} {inv.token} more than invoiced")
    db.commit()
    return "ok"


def reconcile(db: Session) -> dict:
    """Pull deposits and withdrawals from Roqqu and update invoices. Safe to run repeatedly."""
    out = {"deposits": 0, "payouts": 0}
    if settings.mock:
        return out

    live = [Status.AWAITING_PAYMENT.value, Status.FUNDED.value]
    by_addr = {i.deposit_address.lower(): i for i in db.scalars(select(Invoice).where(Invoice.status.in_(live)))}
    if by_addr:
        for d in roqqu.list_deposits():
            inv = by_addr.get(str(d.get("address", "")).lower())
            if inv and d.get("status") == 1:  # 1 = credited
                res = apply_deposit(db, inv, amount=d["value"], token=str(d["token"]),
                                    network=str(d["network"]), tx_hash=str(d["tx_hash"]))
                out["deposits"] += res == "ok"

    pending = db.scalars(select(Invoice).where(Invoice.release_ref.is_not(None), Invoice.release_tx_hash.is_(None))).all()
    if pending:
        sends = {str(w.get("refid")): w for w in roqqu.list_withdrawals()}
        for inv in pending:
            w = sends.get(inv.release_ref)
            if not w:
                continue
            if w.get("tx_hash"):
                inv.release_tx_hash = w["tx_hash"]
                log(db, inv, "payout_confirmed", w["tx_hash"])
                out["payouts"] += 1
            elif w.get("status") in (2, 5) and not db.scalar(
                    select(Event).where(Event.invoice_id == inv.id, Event.kind == "payout_failed")):
                log(db, inv, "payout_failed", f"Roqqu withdrawal status {w.get('status')} (ref {inv.release_ref})")
            db.commit()
    return out


def _reconcile_once() -> None:
    with SessionLocal() as db:
        reconcile(db)


async def poll_roqqu(every: int = 10) -> None:
    while True:
        try:
            await asyncio.to_thread(_reconcile_once)
        except Exception as e:  # keep polling through Roqqu hiccups
            log_.warning("Roqqu poll failed: %s", e)
        await asyncio.sleep(every)


# ---------- schemas ----------

class InvoiceCreate(BaseModel):
    freelancer_email: str
    freelancer_payout_address: str
    client_email: str
    client_name: str = "Astera Client"  # Roqqu needs first and last name to generate a wallet
    description: str
    amount: Decimal = Field(gt=0)
    token: str
    network: str


class ReleaseRequest(BaseModel):
    acknowledge_caution: bool = False


# ---------- routes ----------

STATIC = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/pay/{invoice_id}", include_in_schema=False)
def client_page(invoice_id: str):
    return FileResponse(STATIC / "pay.html")


@app.get("/meta", include_in_schema=False)
def meta():
    return {"mock": settings.mock}


@app.post("/invoices", status_code=201)
def create_invoice(body: InvoiceCreate, db: Session = Depends(get_db)):
    token, network = body.token.lower(), body.network.lower()
    if not safety.is_supported(network, token):
        raise HTTPException(422, "Unsupported token/network. Supported: " +
                            ", ".join(f"{t.upper()} on {n}" for n, t in sorted(safety.SUPPORTED_ASSETS)))
    if not safety.valid_address_format(body.freelancer_payout_address, network):
        raise HTTPException(422, f"Payout address is not a valid {network.upper()} address")

    invoice_id = str(uuid.uuid4())
    parts = body.client_name.split(None, 1)
    customer = {"email": body.client_email, "first_name": parts[0] if parts else "Astera",
                "last_name": parts[1] if len(parts) > 1 else "Client"}
    try:
        deposit_address = roqqu.create_deposit_address(invoice_id, network, token, customer)
    except (NotImplementedError, RuntimeError, ValueError, httpx.HTTPError) as e:
        raise HTTPException(502, f"Could not create a deposit address: {e}")
    # Webhooks are matched to invoices by address, so an address must never serve two live invoices.
    active = [Status.AWAITING_PAYMENT.value, Status.FUNDED.value, Status.DELIVERED.value, Status.DISPUTED.value]
    if db.scalar(select(Invoice).where(Invoice.deposit_address == deposit_address, Invoice.status.in_(active))):
        raise HTTPException(502, "Roqqu returned a deposit address that another active invoice already uses")
    client_key, freelancer_key = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    inv = Invoice(
        id=invoice_id,
        client_key_hash=hash_key(client_key),
        freelancer_key_hash=hash_key(freelancer_key),
        freelancer_email=body.freelancer_email,
        freelancer_payout_address=body.freelancer_payout_address,
        client_email=body.client_email,
        description=body.description,
        amount=body.amount,
        token=token,
        network=network,
        deposit_address=deposit_address,
        status=Status.AWAITING_PAYMENT.value,
        funded_amount=Decimal(0),
        deposit_tx_hashes="",
        expires_at=utcnow() + timedelta(hours=settings.invoice_ttl_hours),
    )
    db.add(inv)
    log(db, inv, "created")
    db.commit()
    # The keys are shown once, here. Only their hashes are stored.
    return {**invoice_out(inv), "client_key": client_key, "freelancer_key": freelancer_key}


@app.get("/invoices/{invoice_id}")
def read_invoice(invoice_id: str, request: Request, db: Session = Depends(get_db)):
    return invoice_out(authed_invoice(db, invoice_id, request, {"client", "freelancer"}))


@app.get("/invoices/{invoice_id}/events")
def read_events(invoice_id: str, request: Request, db: Session = Depends(get_db)):
    authed_invoice(db, invoice_id, request, {"client", "freelancer"})
    rows = db.scalars(select(Event).where(Event.invoice_id == invoice_id).order_by(Event.id)).all()
    return [{"kind": e.kind, "detail": e.detail, "at": e.created_at.isoformat()} for e in rows]


@app.post("/webhooks/roqqu")
async def roqqu_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    if not roqqu.verify_webhook(raw, request.headers):
        raise HTTPException(401, "Invalid webhook signature")
    event = roqqu.parse_webhook(await request.json())
    inv = db.scalar(select(Invoice).where(Invoice.deposit_address == event["address"]))
    if not inv:
        return {"ok": True, "ignored": True}
    result = apply_deposit(db, inv, amount=event["amount"], token=event["token"],
                           network=event["network"], tx_hash=event["tx_hash"])
    return {"ok": True, "result": result}


@app.post("/invoices/{invoice_id}/deliver")
def mark_delivered(invoice_id: str, request: Request, db: Session = Depends(get_db)):
    inv = authed_invoice(db, invoice_id, request, {"freelancer"})
    transition(db, inv, Status.DELIVERED)
    db.commit()
    return invoice_out(inv)


@app.get("/invoices/{invoice_id}/release-check")
def release_check(invoice_id: str, request: Request, db: Session = Depends(get_db)):
    inv = authed_invoice(db, invoice_id, request, {"client"})
    report = safety.check_payout(inv.freelancer_payout_address, inv.network, inv.token,
                                 deposit_address=inv.deposit_address)
    return {
        **report.to_dict(),
        "explanation": explain.narrate(report, fmt(inv.amount), inv.token, inv.network),
    }


@app.post("/invoices/{invoice_id}/release")
def release(invoice_id: str, request: Request, body: ReleaseRequest | None = None, db: Session = Depends(get_db)):
    body = body or ReleaseRequest()
    inv = authed_invoice(db, invoice_id, request, {"client"})
    if inv.status != Status.DELIVERED.value:
        raise HTTPException(409, f"Invoice is {inv.status}; it must be delivered before release")

    report = safety.check_payout(inv.freelancer_payout_address, inv.network, inv.token,
                                 deposit_address=inv.deposit_address)
    log(db, inv, "safety_check", report.verdict)
    explanation = explain.narrate(report, fmt(inv.amount), inv.token, inv.network)

    if report.verdict == "danger":
        db.commit()
        raise HTTPException(409, {"message": "Release blocked by safety check", **report.to_dict(),
                                  "explanation": explanation})
    if report.verdict == "caution" and not body.acknowledge_caution:
        db.commit()
        raise HTTPException(409, {"message": "Caution: resend with acknowledge_caution=true to proceed",
                                  **report.to_dict(), "explanation": explanation})

    try:
        sent = roqqu.send(address=inv.freelancer_payout_address, amount=fmt(inv.amount),
                          token=inv.token, network=inv.network)
    except (NotImplementedError, RuntimeError, httpx.HTTPError) as e:
        log(db, inv, "payout_error", str(e))
        db.commit()
        raise HTTPException(502, f"Payout failed, funds are still in escrow: {e}")
    inv.release_ref = sent.get("ref")
    inv.release_tx_hash = sent.get("tx_hash")
    transition(db, inv, Status.RELEASED)
    db.commit()
    return {**invoice_out(inv), "safety": {**report.to_dict(), "explanation": explanation}}


@app.post("/invoices/{invoice_id}/dispute")
def dispute(invoice_id: str, request: Request, db: Session = Depends(get_db)):
    inv = authed_invoice(db, invoice_id, request, {"client", "freelancer"})
    transition(db, inv, Status.DISPUTED)
    db.commit()
    return invoice_out(inv)


@app.post("/dev/simulate-payment/{invoice_id}")
def simulate_payment(invoice_id: str, request: Request, amount: Decimal | None = None, db: Session = Depends(get_db)):
    """Mock-mode only: pretend the client paid, as if Roqqu's webhook fired."""
    if not settings.mock:
        raise HTTPException(404)
    inv = authed_invoice(db, invoice_id, request, {"client", "freelancer"})
    result = apply_deposit(db, inv, amount=amount or inv.amount, token=inv.token,
                           network=inv.network, tx_hash="0xsim" + uuid.uuid4().hex)
    return {"result": result, **invoice_out(inv)}
