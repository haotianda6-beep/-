import inspect
from typing import Any

import ccxt
from dotenv import load_dotenv

from app.core.credentials import CredentialStore
from app.core.credential_utils import ENV_PATH
from app.core.models import ExchangeName


def build_ccxt_exchange(exchange_name: ExchangeName | str, exchange_id: str, default_type: str, timeout: int = 12000):
    exchange_name = ExchangeName(exchange_name)
    load_dotenv(ENV_PATH, override=False)
    creds = CredentialStore().effective_exchange(exchange_name)
    config: dict[str, Any] = {
        "apiKey": creds.values.get("api_key", ""),
        "secret": creds.values.get("api_secret", ""),
        "enableRateLimit": True,
        "timeout": timeout,
        "options": {"defaultType": default_type},
    }
    passphrase = creds.values.get("passphrase")
    if passphrase:
        config["password"] = passphrase
    exchange = getattr(ccxt, exchange_id)(config)
    _apply_exchange_modes(exchange, exchange_name, creds.use_testnet, creds.use_demo)
    return exchange


def apply_modes_from_credentials(exchange, exchange_name: ExchangeName | str) -> None:
    exchange_name = ExchangeName(exchange_name)
    load_dotenv(ENV_PATH, override=False)
    creds = CredentialStore().effective_exchange(exchange_name)
    _apply_exchange_modes(exchange, exchange_name, creds.use_testnet, creds.use_demo)


def sanitize_exchange_error(message: str) -> str:
    sanitized = message
    for secret in CredentialStore().secret_values():
        sanitized = sanitized.replace(secret, "***")
    return sanitized[:300]


def _apply_exchange_modes(exchange, exchange_name: ExchangeName, use_testnet: bool, use_demo: bool) -> None:
    if use_testnet and hasattr(exchange, "set_sandbox_mode"):
        exchange.set_sandbox_mode(True)
    if exchange_name == ExchangeName.OKX and use_demo:
        exchange.headers = {**getattr(exchange, "headers", {}), "x-simulated-trading": "1"}
    if exchange_name == ExchangeName.BITGET and use_demo:
        exchange.options = {**getattr(exchange, "options", {}), "defaultType": exchange.options.get("defaultType", "swap")}
    if exchange_name == ExchangeName.OKX:
        _patch_okx_empty_market_rows(exchange)


def _patch_okx_empty_market_rows(exchange) -> None:
    if getattr(exchange, "_perp_arb_okx_market_patch", False):
        return
    original_fetch_markets = exchange.fetch_markets

    def valid_markets(markets):
        return [market for market in markets if market.get("id") and market.get("symbol")]

    if inspect.iscoroutinefunction(original_fetch_markets):
        async def fetch_markets(params=None):
            return valid_markets(await original_fetch_markets(params or {}))
    else:
        def fetch_markets(params=None):
            return valid_markets(original_fetch_markets(params or {}))

    exchange.fetch_markets = fetch_markets
    exchange._perp_arb_okx_market_patch = True
