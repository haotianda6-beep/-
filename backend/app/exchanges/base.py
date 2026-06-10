from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Literal

from app.core.models import ExchangeBalance, ExchangeName, PositionSnapshot


class ExchangeAdapter(ABC):
    name: ExchangeName

    @abstractmethod
    async def get_symbols(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def get_ticker(self, symbol: str) -> dict[str, Decimal]:
        raise NotImplementedError

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Decimal:
        raise NotImplementedError

    @abstractmethod
    async def get_balance(self) -> ExchangeBalance:
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> list[PositionSnapshot]:
        raise NotImplementedError

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        raise NotImplementedError

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: Decimal,
        price: Decimal,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> None:
        raise NotImplementedError

