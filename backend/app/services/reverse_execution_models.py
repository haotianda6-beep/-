from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.models import ExchangeName


@dataclass
class ExecutionStep:
    name: str
    status: str = "pending"
    detail: str = ""
    raw: dict[str, Any] | None = None


@dataclass
class ReversePositionRecord:
    id: str
    exchange: ExchangeName
    symbol: str
    base_asset: str
    quantity: Decimal
    borrowed_quantity: Decimal
    spot_entry_price: Decimal
    perp_entry_price: Decimal
    spot_order_id: str | None
    perp_order_id: str | None
    opened_at: datetime
    status: str = "open"


@dataclass
class ExecutionResult:
    id: str
    status: str
    reason: str
    steps: list[ExecutionStep] = field(default_factory=list)
    position: ReversePositionRecord | None = None
