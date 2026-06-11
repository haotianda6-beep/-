from app.core.models import ExchangeName
from app.services.borrow_pool_blocklist import active_borrow_pool_block, active_borrow_pool_reason, is_borrow_pool_error, is_rate_limit_error, mark_borrow_pool_block


def test_borrow_pool_blocklist_marks_symbol_as_not_openable(tmp_path) -> None:
    path = tmp_path / "borrow_pool_blocks.json"

    mark_borrow_pool_block(ExchangeName.BYBIT, "HUSDT", "Borrowing demand is high", path=path)

    assert is_borrow_pool_error("retCode 34022030 Borrowing demand is high")
    assert "借币资金池不足" in active_borrow_pool_reason(ExchangeName.BYBIT, "HUSDT", path=path)
    block = active_borrow_pool_block(ExchangeName.BYBIT, "HUSDT", path=path)
    assert block is not None
    assert block.available_qty == 0


def test_borrow_failure_blocklist_marks_available_zero(tmp_path) -> None:
    path = tmp_path / "borrow_pool_blocks.json"

    mark_borrow_pool_block(ExchangeName.BITGET, "SENTUSDT", "max borrowable amount is 0", path=path)

    block = active_borrow_pool_block(ExchangeName.BITGET, "SENTUSDT", path=path)
    assert block is not None
    assert block.available_qty == 0
    assert "实盘借币失败" in block.reason


def test_rate_limit_blocklist_uses_rate_limit_reason(tmp_path) -> None:
    path = tmp_path / "borrow_pool_blocks.json"

    mark_borrow_pool_block(ExchangeName.BYBIT, "IDUSDT", 'bybit {"retCode":10006,"retMsg":"Too many visits. Exceeded the API Rate Limit."}', path=path)

    assert is_rate_limit_error("Too many visits. Exceeded the API Rate Limit.")
    block = active_borrow_pool_block(ExchangeName.BYBIT, "IDUSDT", path=path)
    assert block is not None
    assert "限频" in block.reason
