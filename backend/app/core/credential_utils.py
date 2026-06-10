import os
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.models import ExchangeName


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = PROJECT_ROOT / ".env"
CONFIG_DIR = PROJECT_ROOT / "config"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.enc"
CREDENTIALS_KEY_PATH = CONFIG_DIR / "credentials.key"

EXCHANGE_ENV_PREFIX = {
    ExchangeName.BINANCE: "BINANCE",
    ExchangeName.OKX: "OKX",
    ExchangeName.GATE: "GATE",
    ExchangeName.BITGET: "BITGET",
    ExchangeName.BYBIT: "BYBIT",
}

EXCHANGE_FIELDS: dict[ExchangeName, tuple[tuple[str, str], ...]] = {
    ExchangeName.BINANCE: (("api_key", "BINANCE_API_KEY"), ("api_secret", "BINANCE_API_SECRET")),
    ExchangeName.OKX: (("api_key", "OKX_API_KEY"), ("api_secret", "OKX_API_SECRET"), ("passphrase", "OKX_API_PASSPHRASE")),
    ExchangeName.GATE: (("api_key", "GATE_API_KEY"), ("api_secret", "GATE_API_SECRET")),
    ExchangeName.BITGET: (("api_key", "BITGET_API_KEY"), ("api_secret", "BITGET_API_SECRET"), ("passphrase", "BITGET_API_PASSPHRASE")),
    ExchangeName.BYBIT: (("api_key", "BYBIT_API_KEY"), ("api_secret", "BYBIT_API_SECRET")),
}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def mask_secret(value: str) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return f"{value[:2]}***"
    return f"{value[:4]}***{value[-4:]}"


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
