from decimal import Decimal

from app.config import Settings
from app.models import Mt4Tick, utc_now_ms
from app.mt4_bridge import Mt4Bridge


def test_mt4_tick_uses_server_receive_time_for_freshness(tmp_path):
    cfg = Settings(_env_file=None, SQLITE_PATH=tmp_path / "test.sqlite3")
    bridge = Mt4Bridge(cfg)

    quote = bridge.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal("4177"), ask=Decimal("4178"), timestamp_ms=-1))

    assert quote.timestamp_ms > utc_now_ms() - 1000
    assert bridge.connected()


def test_mt4_tick_updates_account_snapshot(tmp_path):
    cfg = Settings(_env_file=None, SQLITE_PATH=tmp_path / "test.sqlite3")
    bridge = Mt4Bridge(cfg)

    bridge.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("4177"),
            ask=Decimal("4178"),
            account_balance=Decimal("1000"),
            account_equity=Decimal("1008.5"),
            account_free_margin=Decimal("900"),
            account_margin=Decimal("100"),
            account_profit=Decimal("8.5"),
            account_currency="USD",
        )
    )

    account = bridge.account_snapshot()
    assert account is not None
    assert account.balance == Decimal("1000")
    assert account.equity == Decimal("1008.5")
    assert account.available == Decimal("900")
    assert account.used_margin == Decimal("100")
    assert account.unrealized_pnl == Decimal("8.5")
    assert account.currency == "USD"


def test_mt4_tick_without_account_fields_keeps_account_empty(tmp_path):
    cfg = Settings(_env_file=None, SQLITE_PATH=tmp_path / "test.sqlite3")
    bridge = Mt4Bridge(cfg)

    bridge.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal("4177"), ask=Decimal("4178")))

    assert bridge.account_snapshot() is None
