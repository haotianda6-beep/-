from app.core.credentials import EffectiveExchangeCredentials
from app.core.models import ExchangeName
from app.services.exchange_factory import build_ccxt_exchange


def test_build_ccxt_exchange_accepts_serialized_exchange_name(monkeypatch) -> None:
    captured = {}

    class FakeExchange:
        def __init__(self, config):
            self.config = config
            self.options = config.get("options", {})

    def credentials(_store, exchange):
        captured["exchange"] = exchange
        return EffectiveExchangeCredentials(
            exchange=exchange,
            values={"api_key": "key", "api_secret": "secret"},
            source="test",
            missing_fields=[],
            use_testnet=False,
            use_demo=False,
        )

    monkeypatch.setattr("app.services.exchange_factory.CredentialStore.effective_exchange", credentials)
    monkeypatch.setattr("app.services.exchange_factory.ccxt.bitget", FakeExchange)

    exchange = build_ccxt_exchange("BITGET", "bitget", "swap")

    assert exchange.config["apiKey"] == "key"
    assert exchange.config["options"]["defaultType"] == "swap"
    assert captured["exchange"] == ExchangeName.BITGET


def test_okx_empty_market_rows_are_filtered(monkeypatch) -> None:
    class FakeOkx:
        def __init__(self, config):
            self.config = config
            self.options = config.get("options", {})

        def fetch_markets(self, params=None):
            return [
                {"id": "BTC-USDT-SWAP", "symbol": "BTC/USDT:USDT"},
                {"id": None, "symbol": None},
                {"id": "", "symbol": ""},
            ]

    def credentials(_store, exchange):
        return EffectiveExchangeCredentials(
            exchange=exchange,
            values={"api_key": "key", "api_secret": "secret"},
            source="test",
            missing_fields=[],
            use_testnet=False,
            use_demo=False,
        )

    monkeypatch.setattr("app.services.exchange_factory.CredentialStore.effective_exchange", credentials)
    monkeypatch.setattr("app.services.exchange_factory.ccxt.okx", FakeOkx)

    exchange = build_ccxt_exchange(ExchangeName.OKX, "okx", "swap")

    assert exchange.fetch_markets() == [{"id": "BTC-USDT-SWAP", "symbol": "BTC/USDT:USDT"}]
