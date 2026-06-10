import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.core.models import ExchangeName


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = PROJECT_ROOT / "config" / "borrow_pool_blocks.json"
POOL_PATTERNS = ("34022030", "fund pool", "Borrowing demand is high")


@dataclass(frozen=True)
class BorrowPoolBlock:
    reason: str
    available_qty: Decimal = Decimal("0")


def is_borrow_pool_error(message: str) -> bool:
    return any(pattern.lower() in message.lower() for pattern in POOL_PATTERNS)


def mark_borrow_pool_block(exchange: ExchangeName, symbol: str, reason: str, seconds: int = 900, path: Path | None = None) -> None:
    target = path or DEFAULT_PATH
    state = _read(target)
    key = _key(exchange, symbol)
    state[key] = {
        "exchange": exchange.value if hasattr(exchange, "value") else str(exchange),
        "symbol": symbol,
        "reason": reason[:220],
        "display_reason": _display_reason(reason),
        "available_qty": "0",
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
    }
    _write(target, state)


def active_borrow_pool_block(exchange: ExchangeName, symbol: str, path: Path | None = None) -> BorrowPoolBlock | None:
    item = _active_item(exchange, symbol, path)
    if not item:
        return None
    return BorrowPoolBlock(
        reason=str(item.get("display_reason") or _display_reason(str(item.get("reason") or ""))),
        available_qty=Decimal(str(item.get("available_qty") or "0")),
    )


def active_borrow_pool_reason(exchange: ExchangeName, symbol: str, path: Path | None = None) -> str | None:
    block = active_borrow_pool_block(exchange, symbol, path)
    return block.reason if block else None


def _active_item(exchange: ExchangeName, symbol: str, path: Path | None = None) -> dict | None:
    item = _read(path or DEFAULT_PATH).get(_key(exchange, symbol))
    if not item:
        return None
    try:
        expires_at = datetime.fromisoformat(str(item["expires_at"]))
    except (KeyError, ValueError):
        return None
    if expires_at <= datetime.now(timezone.utc):
        return None
    return item


def _key(exchange: ExchangeName, symbol: str) -> str:
    value = exchange.value if hasattr(exchange, "value") else str(exchange)
    return f"{value}:{symbol}"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _display_reason(reason: str) -> str:
    if is_borrow_pool_error(reason):
        return "借币资金池不足，交易所暂时没有可借库存；已按可借数量 0 处理，冷却期内不作为可开仓机会。"
    return "实盘借币失败，已按可借数量 0 处理；冷却期内不作为可开仓机会。"
