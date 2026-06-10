from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.models import ExchangeName


@dataclass
class CrossSpreadPosition:
    id: str
    symbol: str
    long_exchange: ExchangeName
    short_exchange: ExchangeName
    quantity: Decimal
    long_entry_price: Decimal
    short_entry_price: Decimal
    long_order_id: str | None
    short_order_id: str | None
    opened_at: datetime
    status: str = "open"
