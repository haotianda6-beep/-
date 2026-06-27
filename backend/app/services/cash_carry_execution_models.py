from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.models import ExchangeName


CASH_CARRY_RULESET_VERSION = "v3.0"


@dataclass
class CashCarryPosition:
    id: str
    exchange: ExchangeName
    symbol: str
    base_asset: str
    quantity: Decimal
    spot_entry_price: Decimal
    perp_entry_price: Decimal
    spot_order_id: str | None
    perp_order_id: str | None
    opened_at: datetime
    status: str = "open"
    add_count: int = 0
    last_add_basis_pct: Decimal | None = None
    add_orders: list[dict[str, Any]] = field(default_factory=list)
    rebalance_orders: list[dict[str, Any]] = field(default_factory=list)
    strategy_version: str = CASH_CARRY_RULESET_VERSION
