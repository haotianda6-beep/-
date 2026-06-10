import os
from pathlib import Path

from dotenv import load_dotenv

from app.core.credentials import CredentialStore
from app.core.credential_utils import env_bool
from app.core.models import ExchangeCredentialStatus


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)


def credential_statuses() -> list[ExchangeCredentialStatus]:
    load_dotenv(ENV_PATH, override=False)
    return CredentialStore().exchange_statuses(
        live_data_enabled=env_bool("LIVE_DATA_ENABLED"),
        trading_enabled=env_bool("TRADING_ENABLED"),
        order_execution_enabled=env_bool("ORDER_EXECUTION_ENABLED"),
        read_only_mode=env_bool("API_READ_ONLY_MODE", default=True),
    )


def ai_status() -> dict[str, bool | str]:
    load_dotenv(ENV_PATH, override=False)
    provider = os.getenv("AI_PROVIDER", "").strip().lower()
    store = CredentialStore()
    deepseek = store.deepseek_credentials()
    if provider == "deepseek" or deepseek["api_key"]:
        return {
            "provider": "deepseek",
            "configured": bool(deepseek["api_key"]),
            "base_url": deepseek["base_url"],
            "model": deepseek["model"],
        }
    if provider:
        return {"provider": provider, "configured": False, "base_url": "", "model": ""}
    return {"provider": "", "configured": False, "base_url": "", "model": ""}
