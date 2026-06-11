from decimal import Decimal

from app.core.models import BotSettings, ExchangeName
from app.services.asset_identity import MarketAsset
from app.services.borrow_checker import BorrowCheck
from app.services.borrow_pool_blocklist import mark_borrow_pool_block
from app.services.cash_carry_fast_refresh import CashCarryFastRefresher
from app.services.cash_carry_scanner import CashCarryExchangeData, TradeMarket
from app.services.live_market_types import CashCarryScan
from app.services.reverse_cash_carry_scanner import ReverseCashCarryScanner


def test_reverse_cash_carry_detects_discount_and_negative_funding() -> None:
    scanner = ReverseCashCarryScanner(_borrow_checker())
    item = scanner._build_opportunity("ABCUSDT", _data("99", "-0.0002"), BotSettings(order_notional_usdt=Decimal("100")))

    assert item is not None
    assert item.basis_pct == Decimal("1.0000")
    assert item.funding_rate_pct == Decimal("-0.0200")
    assert item.estimated_funding_income == Decimal("0.0200")
    assert item.estimated_borrow_cost == Decimal("0.0004")
    assert item.borrow_check_status == "ok"
    assert item.blocked_reasons == []
    assert "资金费率不是负数，多头不能收资金费" not in item.blocked_reasons


def test_reverse_cash_carry_candidate_explains_positive_funding_and_low_discount() -> None:
    scanner = ReverseCashCarryScanner(_borrow_checker())
    item = scanner._build_opportunity("ABCUSDT", _data("99.5", "0.0001"), BotSettings())

    assert item is not None
    assert "合约折价未达 0.8%" in item.blocked_reasons
    assert "资金费率不是负数，多头不能收资金费" in item.blocked_reasons


def test_reverse_cash_carry_blocks_same_symbol_with_different_base_id() -> None:
    scanner = ReverseCashCarryScanner(_borrow_checker())
    item = scanner._build_opportunity(
        "ABCUSDT",
        _data("99", "-0.0002", spot_asset=MarketAsset("ABC", "ABCOLD"), swap_asset=MarketAsset("ABC", "ABCNEW")),
        BotSettings(),
    )

    assert item is not None
    assert "合约与现货标的未确认一致" in " / ".join(item.blocked_reasons)


def test_reverse_cash_carry_allows_pre_market_contracts() -> None:
    scanner = ReverseCashCarryScanner(_borrow_checker())
    item = scanner._build_opportunity("ABCUSDT", _data("99", "-0.0002", is_pre_market=True), BotSettings())

    assert item is not None
    assert item.blocked_reasons == []


def test_reverse_cash_carry_blocks_pre_market_when_spot_transfer_is_closed() -> None:
    scanner = ReverseCashCarryScanner(_borrow_checker())
    item = scanner._build_opportunity("ABCUSDT", _data("99", "-0.0002", is_pre_market=True, spot_transfer_closed=True), BotSettings())

    assert item is not None
    assert "预上市合约且现货充提均关闭，禁止自动开仓" in item.blocked_reasons


def test_reverse_cash_carry_blocks_when_borrow_limit_is_too_low() -> None:
    scanner = ReverseCashCarryScanner(_borrow_checker(available_qty=Decimal("0.5")))
    item = scanner._build_opportunity("ABCUSDT", _data("99", "-0.0002"), BotSettings(order_notional_usdt=Decimal("100")))

    assert item is not None
    assert item.borrow_check_status == "blocked"
    assert "可借数量不足" in " / ".join(item.blocked_reasons)


def test_reverse_fast_refresh_does_not_skip_borrow_check() -> None:
    scanner = ReverseCashCarryScanner(_borrow_checker())
    item = scanner._build_opportunity("ABCUSDT", _data("99.5", "-0.0001"), BotSettings(order_notional_usdt=Decimal("100")))

    refreshed = CashCarryFastRefresher(_ticker_cache(), "reverse").refresh(CashCarryScan(candidates=[item]), BotSettings(order_notional_usdt=Decimal("100")))

    assert refreshed.opportunities == []
    assert "借币校验未完成，等待全量扫描" in refreshed.candidates[0].blocked_reasons


def test_reverse_fast_refresh_applies_borrow_pool_block_without_ws_prices(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.borrow_pool_blocklist.DEFAULT_PATH", tmp_path / "borrow_pool_blocks.json")
    mark_borrow_pool_block(ExchangeName.BINANCE, "ABCUSDT", "Borrowing demand is high")
    scanner = ReverseCashCarryScanner(_borrow_checker())
    item = scanner._build_opportunity("ABCUSDT", _data("99", "-0.0002"), BotSettings(order_notional_usdt=Decimal("100")))
    item = item.model_copy(update={"blocked_reasons": [], "borrow_check_status": "ok"})

    refreshed = CashCarryFastRefresher(_empty_ticker_cache(), "reverse").refresh(CashCarryScan(opportunities=[item]), BotSettings(order_notional_usdt=Decimal("100")))

    assert refreshed.opportunities == []
    assert refreshed.candidates[0].borrow_available_qty == Decimal("0.000000")
    assert "借币资金池不足" in " / ".join(refreshed.candidates[0].blocked_reasons)


def test_reverse_fast_refresh_dedupes_rate_limit_block_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.borrow_pool_blocklist.DEFAULT_PATH", tmp_path / "borrow_pool_blocks.json")
    mark_borrow_pool_block(ExchangeName.BINANCE, "ABCUSDT", 'bybit {"retCode":10006,"retMsg":"Too many visits. Exceeded the API Rate Limit."}')
    reason = "交易所 API 限频，系统已短暂冷却该机会；不是可借数量为 0，稍后会自动重试。"
    scanner = ReverseCashCarryScanner(_borrow_checker())
    item = scanner._build_opportunity("ABCUSDT", _data("99", "-0.0002"), BotSettings(order_notional_usdt=Decimal("100")))
    item = item.model_copy(update={"blocked_reasons": [reason, reason], "borrow_check_status": "blocked"})

    refreshed = CashCarryFastRefresher(_empty_ticker_cache(), "reverse").refresh(CashCarryScan(candidates=[item]), BotSettings(order_notional_usdt=Decimal("100")))

    assert refreshed.candidates[0].blocked_reasons.count(reason) == 1


def test_reverse_cash_carry_hides_borrow_pool_shortage_from_opportunities(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.borrow_pool_blocklist.DEFAULT_PATH", tmp_path / "borrow_pool_blocks.json")
    mark_borrow_pool_block(ExchangeName.BINANCE, "ABCUSDT", "Borrowing demand is high")
    scanner = ReverseCashCarryScanner(_borrow_checker())
    item = scanner._build_opportunity("ABCUSDT", _data("99", "-0.0002"), BotSettings(order_notional_usdt=Decimal("100")))

    assert item is not None
    assert item.borrow_check_status == "blocked"
    assert item.borrow_available_qty == Decimal("0.000000")
    assert item.blocked_reasons
    assert "借币资金池不足" in " / ".join(item.blocked_reasons)


class _borrow_checker:
    def __init__(self, available_qty: Decimal = Decimal("5")) -> None:
        self.available_qty = available_qty

    def check(self, exchange_name, code, required_qty, reference_price, hold_hours) -> BorrowCheck:
        blocked = self.available_qty < required_qty
        return BorrowCheck(
            status="blocked" if blocked else "ok",
            available_qty=self.available_qty,
            daily_rate=Decimal("0.000011"),
            rate_period_hours=Decimal("24"),
            estimated_cost_usdt=required_qty * reference_price * Decimal("0.000011") * hold_hours / Decimal("24"),
            term="活期借币，通常无固定到期日",
            risk_tags=["活期借币利率可能浮动"],
            blocked_reasons=[f"可借数量不足，需要 {required_qty}，可借 {self.available_qty}"] if blocked else [],
        )


class _ticker_cache:
    def get(self, exchange, market_type, symbol, max_age_seconds=10):
        if market_type == "spot":
            return {"bid": "100", "quoteVolume": "1000000"}
        return {"ask": "98.5", "quoteVolume": "1000000"}


class _empty_ticker_cache:
    def get(self, exchange, market_type, symbol, max_age_seconds=10):
        return None


def _data(
    perp_ask: str,
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
        spot_tickers={"ABCUSDT": {"bid": "100", "quoteVolume": "1000000"}},
        swap_tickers={"ABCUSDT": {"ask": perp_ask, "quoteVolume": "1000000"}},
        funding_rates={"ABCUSDT": Decimal(funding_rate)},
    )
