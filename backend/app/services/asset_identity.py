from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MarketAsset:
    base: str
    base_id: str


def asset_from_market(market: dict[str, Any]) -> MarketAsset:
    base = _clean(market.get("base"))
    return MarketAsset(base=base, base_id=_clean(market.get("baseId") or base))


def same_local_asset(left: MarketAsset, right: MarketAsset) -> bool:
    if not left.base or not right.base or left.base != right.base:
        return False
    return bool(left.base_id and right.base_id and left.base_id == right.base_id)


def local_identity_reasons(label: str, swap_asset: MarketAsset, spot_asset: MarketAsset | None) -> list[str]:
    if spot_asset is None:
        return []
    if same_local_asset(swap_asset, spot_asset):
        return []
    return [f"{label}: 合约与现货标的未确认一致，禁止开仓"]


def _clean(value: object) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())
