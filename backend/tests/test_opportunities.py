from decimal import Decimal
from datetime import datetime, timedelta, timezone

from app.core.models import BotSettings, CashCarryPositionRow, ExchangeName, OpportunityCandidate
from app.services.asset_identity import MarketAsset
from app.services.arbitrage_engine import ArbitrageEngine
from app.services.fast_opportunity_refresh import FastOpportunityRefresher
from app.services.live_market_types import ExchangeMarketData, SwapMarket
from app.services.live_opportunities import LiveOpportunityScanner
from app.services.market_checks import TransferNetworks, bidirectional_route_check
from app.services.settings_store import SettingsStore


def test_opportunities_apply_spread_funding_volume_and_transfer_filters(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    engine = ArbitrageEngine(store)

    opportunities = engine.get_opportunities(BotSettings())

    assert opportunities
    assert all(item.spread_pct >= Decimal("1.5") for item in opportunities)
    assert all(item.estimated_funding_net >= Decimal("0.01") for item in opportunities)
    assert all(item.min_volume_24h_usdt >= Decimal("300000") for item in opportunities)
    assert all(item.spot_transfer_ok for item in opportunities)
    assert all(item.depth_ok for item in opportunities)


def test_symbol_blacklist_removes_matching_opportunities(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    engine = ArbitrageEngine(store)
    settings = BotSettings(symbol_blacklist=["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"])

    opportunities = engine.get_opportunities(settings)

    assert opportunities == []


def test_fast_refresh_keeps_candidate_blocked_reasons() -> None:
    reason = "现货充值/提现链路未双向确认"
    candidate = OpportunityCandidate(
        symbol="HOMEUSDT",
        long_exchange=ExchangeName.BITGET,
        short_exchange=ExchangeName.BYBIT,
        long_price=Decimal("1"),
        short_price=Decimal("1.1"),
        spread_pct=Decimal("10"),
        long_volume_24h_usdt=Decimal("1000000"),
        short_volume_24h_usdt=Decimal("1000000"),
        min_volume_24h_usdt=Decimal("1000000"),
        estimated_open_close_fee=Decimal("0.2"),
        estimated_funding_net=Decimal("0.1"),
        estimated_net_profit=Decimal("9.9"),
        spot_transfer_ok=False,
        depth_ok=False,
        risk_tags=["blocked"],
        updated_at=datetime.now(timezone.utc),
        blocked_reasons=[reason],
    )
    tickers = {
        "BITGET": {"HOMEUSDT": {"ask": "1", "quoteVolume": "1000000"}},
        "BYBIT": {"HOMEUSDT": {"bid": "1.12", "quoteVolume": "1000000"}},
    }

    refreshed = FastOpportunityRefresher()._refresh_candidate(candidate, tickers, BotSettings())

    assert refreshed is not None
    assert refreshed.blocked_reasons == [reason]
    assert refreshed.spread_pct == Decimal("12.0000")


def test_cross_exchange_blocks_contract_spot_identity_mismatch() -> None:
    scanner = LiveOpportunityScanner()
    long_data = _market_data(ExchangeName.BINANCE, "ask", "100", MarketAsset("ABC", "ABCOLD"))
    short_data = _market_data(ExchangeName.OKX, "bid", "102", MarketAsset("ABC", "ABC"))
    long_data.funding_rates = {"ABCUSDT": Decimal("0")}
    short_data.funding_rates = {"ABCUSDT": Decimal("0.001")}

    item = scanner._build_opportunity("ABCUSDT", long_data, short_data, BotSettings())

    assert item is not None
    assert "合约与现货标的未确认一致" in " / ".join(item[1])


def test_bidirectional_route_check_explains_missing_or_mismatched_networks() -> None:
    missing = bidirectional_route_check("ABC", {}, {}, "OKX", "BYBIT")
    assert not missing.ok
    assert "属于未确认" in missing.reasons[0]

    confirmed_absent = bidirectional_route_check("ABC", {}, {}, "OKX", "BYBIT", True, True)
    assert not confirmed_absent.ok
    assert "按当前接口确认" in confirmed_absent.reasons[0]

    mismatch = bidirectional_route_check(
        "ABC",
        {"ABC": TransferNetworks(deposit={"ETH"}, withdraw={"ETH"})},
        {"ABC": TransferNetworks(deposit={"TRX"}, withdraw={"TRX"})},
        "OKX",
        "BYBIT",
    )
    assert not mismatch.ok
    assert "已确认" in " / ".join(mismatch.reasons)


def test_matched_cash_carry_position_suppresses_stale_execution_warning(tmp_path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    engine = ArbitrageEngine(store)
    monkeypatch.setattr(
        "app.services.arbitrage_engine.recent_execution_results",
        lambda: [{"strategy_id": "cash-carry", "title": "正向期现执行器", "status": "blocked_by_safety_gate", "reason": "GATE SPCXUSDT 当前现货 1.3214559，合约折算 1.98，数量不一致"}],
    )

    events = engine.get_risk_events(BotSettings(), cash_carry_positions=[_cash_position()])

    assert all("数量不一致" not in item.detail for item in events)


def test_expired_borrow_pool_failure_is_not_reported_as_current_risk(tmp_path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    engine = ArbitrageEngine(store)
    old_at = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    monkeypatch.setattr(
        "app.services.arbitrage_engine.recent_execution_results",
        lambda: [{"strategy_id": "reverse-cash-carry", "title": "反向期现执行器", "status": "failed", "reason": "BYBIT 借币资金池不足", "at": old_at}],
    )

    events = engine.get_risk_events(BotSettings())

    assert all("借币资金池不足" not in item.detail for item in events)


def _market_data(exchange: ExchangeName, price_key: str, price: str, swap_asset: MarketAsset) -> ExchangeMarketData:
    return ExchangeMarketData(
        exchange=exchange,
        swaps={"ABCUSDT": SwapMarket("ABCUSDT", "ABC/USDT:USDT", Decimal("0.0005"), swap_asset)},
        spot_markets={"ABCUSDT": MarketAsset("ABC", "ABC")},
        tickers={"ABCUSDT": {price_key: price, "quoteVolume": "1000000"}},
    )


def _cash_position() -> CashCarryPositionRow:
    now = datetime.now(timezone.utc)
    return CashCarryPositionRow(
        exchange=ExchangeName.GATE,
        symbol="SPCXUSDT",
        status="matched",
        spot_quantity=Decimal("1.977805"),
        spot_entry_price=Decimal("151"),
        spot_price=Decimal("152"),
        spot_unrealized_pnl=Decimal("1"),
        perp_side="short",
        perp_contracts=Decimal("198"),
        perp_base_quantity=Decimal("1.98"),
        contract_size=Decimal("0.01"),
        perp_entry_price=Decimal("157"),
        perp_mark_price=Decimal("152"),
        leverage=Decimal("2"),
        perp_unrealized_pnl=Decimal("1"),
        estimated_funding_rate_pct=Decimal("0.01"),
        estimated_funding_income=Decimal("0.03"),
        estimated_open_fee=Decimal("0.1"),
        estimated_close_fee=Decimal("0.1"),
        current_net_profit=Decimal("1.83"),
        quantity_gap=Decimal("-0.002195"),
        basis_pct=Decimal("0.1"),
        updated_at=now,
    )
