from decimal import Decimal

from app.services.order_sizing import fetch_order_snapshot, filled_base_quantity


def test_filled_base_quantity_prefers_cost_divided_by_average() -> None:
    order = {"id": "1", "filled": "99.89498", "cost": "99.89498", "average": "151.12704993"}

    amount = filled_base_quantity(_FakeSpot(), "SPCX/USDT", order, Decimal("0"))

    assert amount.quantize(Decimal("0.000001")) == Decimal("0.661000")


def test_filled_base_quantity_subtracts_base_fee() -> None:
    order = {"id": "1", "cost": "99.968946", "average": "0.0333341", "fees": [{"currency": "BAS", "cost": "2.96901"}]}

    amount = filled_base_quantity(_FakeSpot(), "BAS/USDT", order, Decimal("0"))

    assert amount.quantize(Decimal("0.000001")) == Decimal("2996.030393")


def test_fetch_order_snapshot_uses_final_fill_when_create_response_has_only_id() -> None:
    exchange = _FetchingSpot()

    order = fetch_order_snapshot(exchange, "JCT/USDT", {"id": "spot-open"})

    assert order["average"] == "0.006287635090102"
    assert order["filled"] == "15904.2"
    assert exchange.fetches == ["spot-open"]


class _FakeSpot:
    has = {"fetchOrder": False}


class _FetchingSpot:
    has = {"fetchOrder": True}

    def __init__(self) -> None:
        self.fetches = []

    def fetch_order(self, order_id, symbol):
        self.fetches.append(order_id)
        return {"id": order_id, "average": "0.006287635090102", "filled": "15904.2", "cost": "99.999806"}
