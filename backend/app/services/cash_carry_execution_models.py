from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.models import ExchangeName


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
