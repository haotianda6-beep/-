from decimal import Decimal

from app.core.models import BotSettings, ExchangeName
from app.services.asset_identity import MarketAsset
from app.services.cash_carry_fast_refresh import CashCarryFastRefresher
from app.services.cash_carry_scanner import CashCarryExchangeData, CashCarryScanner, TradeMarket
from app.services.live_market_types import CashCarryScan


def test_cash_carry_opportunity_accepts_positive_basis_and_funding() -> None:
    scanner = CashCarryScanner()
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002"), BotSettings(order_notional_usdt=Decimal("100")))

    assert item is not None
    assert item.blocked_reasons == []
    assert item.basis_pct == Decimal("1.0000")
    assert item.funding_rate_pct == Decimal("0.0200")
    assert item.estimated_funding_income == Decimal("0.0200")


def test_cash_carry_candidate_explains_negative_funding_and_low_basis() -> None:
    scanner = CashCarryScanner()
    item = scanner._build_opportunity("ABCUSDT", _data("100.5", "-0.0001"), BotSettings())

    assert item is not None
    assert "合约溢价未达 0.8%" in item.blocked_reasons
    assert "资金费率不是正数，空头不能收资金费" in item.blocked_reasons


def test_cash_carry_applies_strategy_specific_volume_threshold() -> None:
    scanner = CashCarryScanner()
    settings = BotSettings(cash_carry_min_volume_usdt=Decimal("2000000"))
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002"), settings)

    assert item is not None
    assert "现货/合约最低24h成交量低于 2000000U" in item.blocked_reasons


def test_cash_carry_blocks_same_symbol_with_different_base_id() -> None:
    scanner = CashCarryScanner()
    item = scanner._build_opportunity(
        "ABCUSDT",
        _data("101", "0.0002", spot_asset=MarketAsset("ABC", "ABCOLD"), swap_asset=MarketAsset("ABC", "ABCNEW")),
        BotSettings(),
    )

    assert item is not None
    assert "合约与现货标的未确认一致" in " / ".join(item.blocked_reasons)


def test_cash_carry_allows_pre_market_contracts() -> None:
    scanner = CashCarryScanner()
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002", is_pre_market=True), BotSettings())

    assert item is not None
    assert item.blocked_reasons == []


def test_cash_carry_blocks_pre_market_when_spot_transfer_is_closed() -> None:
    scanner = CashCarryScanner()
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002", is_pre_market=True, spot_transfer_closed=True), BotSettings())

    assert item is not None
    assert "预上市合约且现货充提均关闭，禁止自动开仓" in item.blocked_reasons


def test_cash_carry_fast_refresh_uses_ws_prices() -> None:
    scanner = CashCarryScanner()
    item = scanner._build_opportunity("ABCUSDT", _data("101", "0.0002"), BotSettings(order_notional_usdt=Decimal("100")))

    refreshed = CashCarryFastRefresher(_ticker_cache(), "forward").refresh(CashCarryScan(candidates=[item]), BotSettings(order_notional_usdt=Decimal("100")))

    assert refreshed.opportunities
    assert refreshed.opportunities[0].basis_pct == Decimal("1.5000")
    assert refreshed.opportunities[0].estimated_net_profit == Decimal("1.2200")


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
