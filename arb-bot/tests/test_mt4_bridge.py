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
