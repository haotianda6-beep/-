from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from dotenv import load_dotenv

from app.core.env import ENV_PATH, credential_statuses, env_bool
from app.core.models import ExchangeBalance, ExchangeName, PositionSnapshot
from app.services.cash_carry_scope import CASH_CARRY_EXCHANGE_SET
from app.services.exchange_factory import build_ccxt_exchange, sanitize_exchange_error


EXCHANGE_IDS = {
    ExchangeName.BINANCE: "binanceusdm",
    ExchangeName.OKX: "okx",
    ExchangeName.GATE: "gateio",
    ExchangeName.BITGET: "bitget",
    ExchangeName.BYBIT: "bybit",
}

@dataclass
class LiveAccountSnapshot:
    balances: list[ExchangeBalance] = field(default_factory=list)
    positions: list[PositionSnapshot] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def decimal_from(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


class LiveReadService:
    def fetch_account_snapshot(self) -> LiveAccountSnapshot:
        load_dotenv(ENV_PATH, override=False)
        snapshot = LiveAccountSnapshot()
        for status in credential_statuses():
            exchange_name = ExchangeName(status.exchange)
            if exchange_name not in CASH_CARRY_EXCHANGE_SET:
                continue
            if not status.configured:
                snapshot.issues.append(f"{status.exchange}: API 凭证未配置完整")
                continue
            exchange = None
            try:
                exchange = self._build_exchange(exchange_name)
                snapshot.balances.append(self._fetch_balance(exchange, exchange_name))
                snapshot.positions.extend(self._fetch_positions(exchange, exchange_name))
            except Exception as exc:  # noqa: BLE001 - exchange libraries raise many custom errors.
                snapshot.issues.append(f"{status.exchange}: {self._sanitize_error(str(exc))}")
            finally:
                self._close_exchange(exchange)
        return snapshot

    def live_data_enabled(self) -> bool:
        load_dotenv(ENV_PATH, override=False)
        return env_bool("LIVE_DATA_ENABLED")

    def _build_exchange(self, exchange_name: ExchangeName):
        return build_ccxt_exchange(exchange_name, EXCHANGE_IDS[exchange_name], "swap", timeout=12000)

    def _close_exchange(self, exchange) -> None:
        close = getattr(exchange, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _fetch_balance(self, exchange, exchange_name: ExchangeName) -> ExchangeBalance:
        raw = exchange.fetch_balance()
        if exchange_name == ExchangeName.GATE:
            gate_balance = self._parse_gate_balance(raw)
            if gate_balance:
                return gate_balance
        usdt = raw.get("USDT", {})
        total = decimal_from(usdt.get("total"))
        free = decimal_from(usdt.get("free"))
        used = decimal_from(usdt.get("used"))
        if used < 0:
            used = Decimal("0")
        if total < free + used:
            total = free + used
        return ExchangeBalance(
            exchange=exchange_name,
            equity_usdt=total,
            available_usdt=free,
            margin_used_usdt=used,
            updated_at=datetime.now(timezone.utc),
        )

    def _parse_gate_balance(self, raw: dict[str, Any]) -> ExchangeBalance | None:
        info = raw.get("info")
        if not isinstance(info, list) or not info:
            return None
        item = next((entry for entry in info if entry.get("currency") == "USDT"), info[0])
        available = decimal_from(item.get("available") or item.get("cross_available"))
        position_margin = decimal_from(item.get("position_initial_margin") or item.get("position_margin"))
        order_margin = decimal_from(item.get("order_margin") or item.get("cross_order_margin"))
        unrealized = decimal_from(item.get("unrealised_pnl"))
        used = max(position_margin + order_margin, Decimal("0"))
        computed_total = available + used + unrealized
        reported_total = decimal_from(item.get("total"))
        total = reported_total if reported_total >= computed_total else computed_total
        return ExchangeBalance(
            exchange=ExchangeName.GATE,
            equity_usdt=total,
            available_usdt=available,
            margin_used_usdt=used,
            updated_at=datetime.now(timezone.utc),
        )

    def _fetch_positions(self, exchange, exchange_name: ExchangeName) -> list[PositionSnapshot]:
        if not exchange.has.get("fetchPositions"):
            return []
        positions = exchange.fetch_positions()
        parsed: list[PositionSnapshot] = []
        for item in positions:
            quantity = decimal_from(
                item.get("contracts")
                or item.get("contractSize")
                or item.get("info", {}).get("positionAmt")
                or item.get("info", {}).get("size")
            )
            if quantity == 0:
                continue
            side = self._parse_side(item)
            if side is None:
                continue
            parsed.append(
                PositionSnapshot(
                    exchange=exchange_name,
                    symbol=self._normalize_symbol(item.get("symbol") or item.get("info", {}).get("symbol", "")),
                    side=side,
                    quantity=abs(quantity),
                    entry_price=decimal_from(item.get("entryPrice")),
                    mark_price=decimal_from(item.get("markPrice")),
                    leverage=decimal_from(item.get("leverage"), "1"),
                    unrealized_pnl=decimal_from(item.get("unrealizedPnl")),
                    liquidation_price=decimal_from(item.get("liquidationPrice")) if item.get("liquidationPrice") else None,
                )
            )
        return parsed

    def _parse_side(self, item: dict[str, Any]) -> str | None:
        side = str(item.get("side") or item.get("info", {}).get("side") or "").lower()
        if side in {"long", "buy"}:
            return "long"
        if side in {"short", "sell"}:
            return "short"
        amount = decimal_from(item.get("info", {}).get("positionAmt"))
        if amount > 0:
            return "long"
        if amount < 0:
            return "short"
        return None

    def _normalize_symbol(self, symbol: str) -> str:
        if "/" not in symbol:
            return symbol
        base = symbol.split("/", 1)[0]
        quote = symbol.split("/", 1)[1].split(":", 1)[0]
        return f"{base}{quote}"

    def _sanitize_error(self, message: str) -> str:
        return sanitize_exchange_error(message)
