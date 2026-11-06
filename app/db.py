import warnings
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings

# SQLite stores Decimals as floats; fine for a hackathon, use Postgres for real money.
warnings.filterwarnings("ignore", message=".*does \\*not\\* support Decimal.*")

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    freelancer_email: Mapped[str] = mapped_column(String(255))
    freelancer_payout_address: Mapped[str] = mapped_column(String(128))
    client_email: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    token: Mapped[str] = mapped_column(String(16))
    network: Mapped[str] = mapped_column(String(16))
    deposit_address: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    funded_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=Decimal(0))
    deposit_tx_hashes: Mapped[str] = mapped_column(Text, default="")  # comma-separated, for idempotency
    client_key_hash: Mapped[str] = mapped_column(String(64))      # sha256 of the client's access key
    freelancer_key_hash: Mapped[str] = mapped_column(String(64))  # sha256 of the freelancer's access key
    release_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # Roqqu send refid
    release_tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Event(Base):
    """Audit trail: every state change, webhook, and safety verdict is logged."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def init_db() -> None:
    Base.metadata.create_all(engine)
