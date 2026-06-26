from decimal import Decimal

from app.config import Settings
from app import mt4_bridge as mt4_bridge_module
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


def test_mt4_tick_updates_trade_status(tmp_path):
    cfg = Settings(_env_file=None, SQLITE_PATH=tmp_path / "test.sqlite3")
    bridge = Mt4Bridge(cfg)

    bridge.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("4177"),
            ask=Decimal("4178"),
            trade_allowed=True,
            trade_context_busy=False,
        )
    )

    assert bridge.trade_allowed() is True
    assert bridge.trade_context_busy() is False


def test_mt4_tick_combines_symbol_and_terminal_trade_status(tmp_path):
    cfg = Settings(_env_file=None, SQLITE_PATH=tmp_path / "test.sqlite3")
    bridge = Mt4Bridge(cfg)

    bridge.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("4177"),
            ask=Decimal("4178"),
            trade_allowed=True,
            symbol_trade_allowed=True,
            terminal_trade_allowed=False,
        )
    )

    assert bridge.trade_allowed() is False


def test_mt4_tick_updates_ea_version(tmp_path):
    cfg = Settings(_env_file=None, SQLITE_PATH=tmp_path / "test.sqlite3")
    bridge = Mt4Bridge(cfg)

    bridge.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            ea_version="20260626-trade-guard",
            bid=Decimal("4177"),
            ask=Decimal("4178"),
        )
    )

    assert bridge.ea_version() == "20260626-trade-guard"


def test_mt4_recent_move_budget_uses_in_memory_quote_window(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, SQLITE_PATH=tmp_path / "test.sqlite3")
    bridge = Mt4Bridge(cfg)
    now = 1_000_000
    bids = [
        Decimal("4000.0"),
        Decimal("4000.2"),
        Decimal("4001.0"),
        Decimal("4001.1"),
        Decimal("4001.6"),
        Decimal("4001.7"),
        Decimal("4002.0"),
        Decimal("4002.4"),
        Decimal("4002.5"),
    ]

    for index, bid in enumerate(bids):
        monkeypatch.setattr(mt4_bridge_module, "utc_now_ms", lambda value=now + index * 1000: value)
        bridge.update_tick(Mt4Tick(symbol="XAUUSD", bid=bid, ask=bid + Decimal("0.3")))

    monkeypatch.setattr(mt4_bridge_module, "utc_now_ms", lambda: now + 8_000)

    assert bridge.recent_move_budget(lookback_ms=10_000, percentile=70, min_points=8) == Decimal("0.3")
    assert bridge.recent_move_budget(lookback_ms=2_000, percentile=70, min_points=8) is None
