import os

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    roqqu_api_key: str = os.getenv("ROQQU_API_KEY", "")
    roqqu_mode: str = os.getenv("ROQQU_MODE", "test")  # "test" or "live"
    roqqu_webhook_secret: str = os.getenv("ROQQU_WEBHOOK_SECRET", "")
    mock: bool = _flag("ASTERA_MOCK", "1")
    external_checks: bool = _flag("ASTERA_EXTERNAL_CHECKS", "1")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./astera.db")
    invoice_ttl_hours: int = int(os.getenv("INVOICE_TTL_HOURS", "48"))
    rpc_urls: dict = {
        "bep20": os.getenv("RPC_BEP20", "https://bsc-dataseed.binance.org"),
        "base": os.getenv("RPC_BASE", "https://mainnet.base.org"),
    }


settings = Settings()
