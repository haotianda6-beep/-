from cryptography.fernet import Fernet

from app.core.credentials import CredentialStore
from app.core.models import ExchangeCredentialInput, ExchangeName


def test_exchange_credentials_are_encrypted_and_masked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.delenv("OKX_API_KEY", raising=False)
    monkeypatch.delenv("OKX_API_SECRET", raising=False)
    monkeypatch.delenv("OKX_API_PASSPHRASE", raising=False)
    store = CredentialStore(tmp_path / "credentials.enc")

    store.save_exchange(
        ExchangeName.OKX,
        ExchangeCredentialInput(api_key="okx-key-123456", api_secret="okx-secret-123456", passphrase="okx-pass-123456"),
    )

    raw = (tmp_path / "credentials.enc").read_bytes()
    assert b"okx-secret-123456" not in raw
    status = store.exchange_statuses(True, True, True, False)[0]
    assert status.exchange == ExchangeName.OKX
    assert status.configured
    assert status.source == "vault"
    assert status.masked_api_key == "okx-***3456"

    effective = store.effective_exchange(ExchangeName.OKX)
    assert effective.values["api_secret"] == "okx-secret-123456"
    assert effective.missing_fields == []


def test_exchange_credentials_fall_back_to_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("BINANCE_API_KEY", "binance-key-123456")
    monkeypatch.setenv("BINANCE_API_SECRET", "binance-secret-123456")
    store = CredentialStore(tmp_path / "credentials.enc")

    status = store.exchange_statuses(True, False, False, True)[4]

    assert status.exchange == ExchangeName.BINANCE
    assert status.configured
    assert status.source == "env"
    assert status.masked_api_key == "bina***3456"
