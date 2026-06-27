from decimal import Decimal
from pathlib import Path

from app.core.models import BotSettings, ExchangeName
from app.services.asset_identity import MarketAsset
from app.services.cash_carry_fast_refresh import CashCarryFastRefresher
from app.services.cash_carry_execution_models import CASH_CARRY_RULESET_VERSION
from app.services.cash_carry_history_quality import CashCarryHistoryQuality
from app.services.cash_carry_quality import entry_net_floor
from app.services.cash_carry_scope import CASH_CARRY_INTERNAL_CANDIDATE_LIMIT
from app.services.cash_carry_scanner import CashCarryExchangeData, CashCarryScanner, TradeMarket
from app.services.live_market_types import CashCarryScan


EMPTY_HISTORY = Path(__file__).with_name("missing_cash_carry_history.json")


def _scanner() -> CashCarryScanner:
    return CashCarryScanner(CashCarryHistoryQuality(EMPTY_HISTORY))


def _fast_refresher() -> CashCarryFastRefresher:
    return CashCarryFastRefresher(_ticker_cache(), CashCarryHistoryQuality(EMPTY_HISTORY))


def test_cash_carry_opportunity_accepts_positive_basis_and_funding() -> None:
    scanner = _scanner()
    item = scanner._build_opportunity("ABCUSDT", _data("101.5", "0.0002"), BotSettings(order_notional_usdt=Decimal("100")))

    assert item is not None
    assert item.blocked_reasons == []
    assert item.basis_pct == Decimal("1.5000")
    assert item.funding_rate_pct == Decimal("0.0200")
    assert item.estimated_funding_income == Decimal("0.0200")


def test_cash_carry_candidate_explains_negative_funding_and_low_basis() -> None:
    scanner = _scanner()
    item = scanner._build_opportunity("ABCUSDT", _data("100.5", "-0.0001"), BotSettings())

    assert item is not None
    assert "合约溢价未达 0.8%" in item.blocked_reasons
    assert "资金费率不是正数，空头不能收资金费" in item.blocked_reasons


def test_cash_carry_applies_strategy_specific_volume_threshold() -> None:
    scanner = _scanner()
    settings = BotSettings(cash_carry_min_volume_usdt=Decimal("2000000"))
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002"), settings)

    assert item is not None
    assert "现货/合约最低24h成交量低于 2000000U" in item.blocked_reasons


def test_cash_carry_symbol_blacklist_accepts_base_asset_name() -> None:
    scanner = _scanner()
    data = _data("101", "0.0002")
    settings = BotSettings(symbol_blacklist=["ABC"])

    rows = scanner._exchange_opportunities(data, settings)

    assert rows == []


def test_cash_carry_scan_only_uses_gate_and_bitget(monkeypatch) -> None:
    scanner = _scanner()
    loaded: list[ExchangeName] = []

    def fake_load(exchange: ExchangeName) -> CashCarryExchangeData:
        loaded.append(exchange)
        return CashCarryExchangeData(exchange=exchange)

    monkeypatch.setattr(scanner, "_load_exchange_data", fake_load)

    scanner.scan(BotSettings())

    assert loaded == [ExchangeName.GATE, ExchangeName.BITGET]


def test_cash_carry_defaults_to_one_position_per_exchange() -> None:
    assert BotSettings().cash_carry_max_positions_per_exchange == 1


def test_cash_carry_scan_respects_blacklist_inside_allowed_exchanges(monkeypatch) -> None:
    scanner = _scanner()
    loaded: list[ExchangeName] = []

    def fake_load(exchange: ExchangeName) -> CashCarryExchangeData:
        loaded.append(exchange)
        return CashCarryExchangeData(exchange=exchange)

    monkeypatch.setattr(scanner, "_load_exchange_data", fake_load)

    scanner.scan(BotSettings(exchange_blacklist=[ExchangeName.GATE, ExchangeName.BYBIT]))

    assert loaded == [ExchangeName.BITGET]


def test_cash_carry_scan_keeps_expanded_internal_candidate_pool(monkeypatch) -> None:
    scanner = _scanner()
    base = scanner._build_opportunity("ABCUSDT", _data("100.5", "-0.0001"), BotSettings())
    assert base is not None
    rows = [base.model_copy(update={"symbol": f"ABC{i}USDT"}) for i in range(CASH_CARRY_INTERNAL_CANDIDATE_LIMIT + 10)]

    monkeypatch.setattr(scanner, "_load_exchange_data", lambda exchange: CashCarryExchangeData(exchange=exchange))
    monkeypatch.setattr(scanner, "_exchange_opportunities", lambda _data, _settings: rows)

    scan = scanner.scan(BotSettings())

    assert len(scan.candidates) == CASH_CARRY_INTERNAL_CANDIDATE_LIMIT


def test_cash_carry_blocks_same_symbol_with_different_base_id() -> None:
    scanner = _scanner()
    item = scanner._build_opportunity(
        "ABCUSDT",
        _data("101", "0.0002", spot_asset=MarketAsset("ABC", "ABCOLD"), swap_asset=MarketAsset("ABC", "ABCNEW")),
        BotSettings(),
    )

    assert item is not None
    assert "合约与现货标的未确认一致" in " / ".join(item.blocked_reasons)


def test_cash_carry_allows_pre_market_contracts() -> None:
    scanner = _scanner()
    item = scanner._build_opportunity("ABCUSDT", _data("101.5", "0.0002", is_pre_market=True), BotSettings())

    assert item is not None
    assert item.blocked_reasons == []


def test_cash_carry_blocks_abnormally_high_entry_basis() -> None:
    scanner = _scanner()
    item = scanner._build_opportunity("ABCUSDT", _data("104", "0.0002"), BotSettings())

    assert item is not None
    assert "开仓基差异常过高" in " / ".join(item.blocked_reasons)


def test_cash_carry_blocks_pre_market_when_spot_transfer_is_closed() -> None:
    scanner = _scanner()
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002", is_pre_market=True, spot_transfer_closed=True), BotSettings())

    assert item is not None
    assert "预上市合约且现货充提均关闭，禁止自动开仓" in item.blocked_reasons


def test_cash_carry_fast_refresh_uses_ws_prices() -> None:
    scanner = _scanner()
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002"), BotSettings(order_notional_usdt=Decimal("100")))

    refreshed = _fast_refresher().refresh(CashCarryScan(candidates=[item]), BotSettings(order_notional_usdt=Decimal("100")))

    assert refreshed.opportunities
    assert refreshed.opportunities[0].basis_pct == Decimal("1.5000")
    assert refreshed.opportunities[0].estimated_net_profit == Decimal("1.0200")


def test_cash_carry_blocks_low_stable_net_profit() -> None:
    scanner = _scanner()
    settings = BotSettings(order_notional_usdt=Decimal("300"))

    item = scanner._build_opportunity("ABCUSDT", _data("100.7", "0.0002"), settings)

    assert item is not None
    assert "稳定开仓安全垫" in " / ".join(item.blocked_reasons)


def test_cash_carry_v3_entry_floor_caps_legacy_percent_for_frequency() -> None:
    settings = BotSettings(
        order_notional_usdt=Decimal("300"),
        cash_carry_min_entry_net_pct=Decimal("0.8"),
        cash_carry_v3_min_profit_pct=Decimal("0.2"),
        max_slippage_pct=Decimal("0.01"),
    )

    assert entry_net_floor(settings) == Decimal("1.20")


def test_cash_carry_blocks_symbol_with_loss_history(tmp_path) -> None:
    state = tmp_path / "cash_carry_execution_state.json"
    state.write_text(
        '{"positions":[{"exchange":"GATE","symbol":"ABCUSDT","status":"closed","close_reason":"GATE ABCUSDT 合约腿已被交易所强平","history":{"actual_net_profit":"-2.5","external_close_type":"liquidation"}}]}',
        encoding="utf-8",
    )
    scanner = CashCarryScanner(CashCarryHistoryQuality(state))
    data = _data("101", "0.0002")
    data.exchange = ExchangeName.GATE

    item = scanner._build_opportunity("ABCUSDT", data, BotSettings(order_notional_usdt=Decimal("300")))

    assert item is not None
    reasons = " / ".join(item.blocked_reasons)
    assert "历史发生过强平" in reasons
    assert "历史累计真实净利 -2.5000U" in reasons


def test_cash_carry_global_history_gate_raises_entry_floor_after_low_win_rate(tmp_path) -> None:
    state = tmp_path / "cash_carry_execution_state.json"
    state.write_text(
        '{"positions":['
        + ",".join(
            f'{{"exchange":"GATE","symbol":"OLD{i}USDT","status":"closed","strategy_version":"{CASH_CARRY_RULESET_VERSION}","history":{{"actual_net_profit":"-1"}}}}'
            for i in range(10)
        )
        + "]}",
        encoding="utf-8",
    )
    scanner = CashCarryScanner(CashCarryHistoryQuality(state))
    data = _data("101.5", "0.0002")
    data.exchange = ExchangeName.GATE
    settings = BotSettings(order_notional_usdt=Decimal("300"))

    item = scanner._build_opportunity("ABCUSDT", data, settings)

    assert item is not None
    reasons = " / ".join(item.blocked_reasons)
    assert "V3历史胜率保护" in reasons
    assert "动态安全垫" in reasons


def test_cash_carry_global_history_gate_ignores_legacy_ruleset(tmp_path) -> None:
    state = tmp_path / "cash_carry_execution_state.json"
    state.write_text(
        '{"positions":['
        + ",".join(
            f'{{"exchange":"GATE","symbol":"OLD{i}USDT","status":"closed","history":{{"actual_net_profit":"-1"}}}}'
            for i in range(10)
        )
        + "]}",
        encoding="utf-8",
    )
    scanner = CashCarryScanner(CashCarryHistoryQuality(state))
    data = _data("101.5", "0.0002")
    data.exchange = ExchangeName.GATE
    settings = BotSettings(order_notional_usdt=Decimal("300"))

    item = scanner._build_opportunity("ABCUSDT", data, settings)

    assert item is not None
    assert "V3历史胜率保护" not in " / ".join(item.blocked_reasons)


def test_cash_carry_global_history_gate_can_be_disabled(tmp_path) -> None:
    state = tmp_path / "cash_carry_execution_state.json"
    state.write_text(
        '{"positions":['
        + ",".join(
            f'{{"exchange":"GATE","symbol":"OLD{i}USDT","status":"closed","strategy_version":"{CASH_CARRY_RULESET_VERSION}","history":{{"actual_net_profit":"-1"}}}}'
            for i in range(10)
        )
        + "]}",
        encoding="utf-8",
    )
    scanner = CashCarryScanner(CashCarryHistoryQuality(state))
    data = _data("101.5", "0.0002")
    data.exchange = ExchangeName.GATE
    settings = BotSettings(order_notional_usdt=Decimal("300"), cash_carry_adaptive_quality_enabled=False)

    item = scanner._build_opportunity("ABCUSDT", data, settings)

    assert item is not None
    assert "V3历史胜率保护" not in " / ".join(item.blocked_reasons)


def test_cash_carry_v3_sort_prefers_higher_quality_signal() -> None:
    scanner = _scanner()
    settings = BotSettings(order_notional_usdt=Decimal("300"))
    low_quality = scanner._build_opportunity("ABCUSDT", _data("101.8", "0.00001"), settings)
    high_quality = scanner._build_opportunity("ABCUSDT", _data("101.35", "0.0005"), settings)

    assert low_quality is not None
    assert high_quality is not None
    low_quality = low_quality.model_copy(update={
        "symbol": "LOWUSDT",
        "estimated_net_profit": Decimal("2.8"),
        "spot_volume_24h_usdt": Decimal("300000"),
        "perp_volume_24h_usdt": Decimal("300000"),
    })
    high_quality = high_quality.model_copy(update={
        "symbol": "HIGHUSDT",
        "estimated_net_profit": Decimal("2.8"),
        "spot_volume_24h_usdt": Decimal("3000000"),
        "perp_volume_24h_usdt": Decimal("3000000"),
    })

    ranked = sorted([low_quality, high_quality], key=lambda item: scanner._candidate_sort_key(item, settings))

    assert ranked[0].symbol == "HIGHUSDT"


def test_cash_carry_candidate_sort_demotes_hard_blocked_large_basis() -> None:
    scanner = _scanner()
    settings = BotSettings(order_notional_usdt=Decimal("300"))
    soft = scanner._build_opportunity("ABCUSDT", _data("100.7", "0.0002"), settings)
    hard = scanner._build_opportunity("ABCUSDT", _data("154", "0.0002"), settings)

    assert soft is not None
    assert hard is not None
    soft = soft.model_copy(update={
        "symbol": "SOFTUSDT",
        "estimated_net_profit": Decimal("1.8"),
        "blocked_reasons": ["合约溢价未达 0.8%", "回归到平仓线后的净利预估 1.8000U < 稳定开仓安全垫 2.4000U"],
    })
    hard = hard.model_copy(update={
        "symbol": "HARDUSDT",
        "estimated_net_profit": Decimal("160"),
        "blocked_reasons": ["现货/合约最低24h成交量低于 300000U", "开仓基差异常过高 54.0000% > 3.0000%，不追异常盘"],
    })

    ranked = sorted([hard, soft], key=lambda item: scanner._candidate_sort_key(item, settings))

    assert ranked[0].symbol == "SOFTUSDT"


def test_cash_carry_fast_refresh_uses_v3_candidate_sort() -> None:
    scanner = _scanner()
    settings = BotSettings(order_notional_usdt=Decimal("300"))
    soft = scanner._build_opportunity("ABCUSDT", _data("100.7", "0.0002"), settings)
    hard = scanner._build_opportunity("ABCUSDT", _data("154", "0.0002"), settings)

    assert soft is not None
    assert hard is not None
    soft = soft.model_copy(update={
        "symbol": "SOFTUSDT",
        "estimated_net_profit": Decimal("1.8"),
        "blocked_reasons": ["合约溢价未达 0.8%", "回归到平仓线后的净利预估 1.8000U < 稳定开仓安全垫 2.4000U"],
    })
    hard = hard.model_copy(update={
        "symbol": "HARDUSDT",
        "estimated_net_profit": Decimal("160"),
        "blocked_reasons": ["现货/合约最低24h成交量低于 300000U", "开仓基差异常过高 54.0000% > 3.0000%，不追异常盘"],
    })

    refresher = _fast_refresher()
    ranked = sorted([hard, soft], key=lambda item: refresher._candidate_sort_key(item, settings))

    assert ranked[0].symbol == "SOFTUSDT"


def test_cash_carry_fast_refresh_drops_blacklisted_symbol() -> None:
    scanner = _scanner()
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002"), BotSettings(order_notional_usdt=Decimal("100")))

    refreshed = _fast_refresher().refresh(CashCarryScan(opportunities=[item]), BotSettings(symbol_blacklist=["ABC"]))

    assert refreshed.opportunities == []
    assert refreshed.candidates == []


def test_cash_carry_depth_zero_blocks_ready_opportunity() -> None:
    scanner = _scanner()
    settings = BotSettings(order_notional_usdt=Decimal("500"), max_slippage_pct=Decimal("0.2"), min_funding_net_usdt=Decimal("0.01"))
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002"), settings)
    assert item is not None
    data = _data("101", "0.0002")
    data.spot_exchange = _BookExchange(asks=[[100, 1]], bids=[[99, 1]])
    data.swap_exchange = _BookExchange(asks=[[102, 1]], bids=[[101, 1]])

    checked = scanner._with_depth_estimate(item, data, settings)

    assert checked.max_safe_notional_usdt == Decimal("100.00")
    assert "盘口深度不足" in " / ".join(checked.blocked_reasons)


def _data(
    perp_bid: str,
    funding_rate: str,
    spot_asset: MarketAsset = MarketAsset("ABC", "ABC"),
    swap_asset: MarketAsset = MarketAsset("ABC", "ABC"),
    is_pre_market: bool = False,
    spot_transfer_closed: bool = False,
) -> CashCarryExchangeData:
    return CashCarryExchangeData(
        exchange=ExchangeName.BINANCE,
        spot_markets={
            "ABCUSDT": TradeMarket(
                "ABCUSDT",
                "ABC/USDT",
                Decimal("0.001"),
                spot_asset,
                deposit_enabled=False if spot_transfer_closed else None,
                withdraw_enabled=False if spot_transfer_closed else None,
            )
        },
        swap_markets={"ABCUSDT": TradeMarket("ABCUSDT", "ABC/USDT:USDT", Decimal("0.0005"), swap_asset, is_pre_market)},
        spot_tickers={"ABCUSDT": {"ask": "100", "quoteVolume": "1000000"}},
        swap_tickers={"ABCUSDT": {"bid": perp_bid, "quoteVolume": "1000000"}},
        funding_rates={"ABCUSDT": Decimal(funding_rate)},
    )


class _ticker_cache:
    def subscribe(self, exchange, market_type, symbol, ccxt_symbol) -> None:
        return None

    def get(self, exchange, market_type, symbol, max_age_seconds=10):
        if market_type == "spot":
            return {"ask": "100", "quoteVolume": "1000000"}
        return {"bid": "101.5", "quoteVolume": "1000000"}


class _BookExchange:
    def __init__(self, asks, bids) -> None:
        self.asks = asks
        self.bids = bids

    def fetch_order_book(self, symbol, limit=50):
        return {"asks": self.asks, "bids": self.bids}

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"contractSize": "1"}
