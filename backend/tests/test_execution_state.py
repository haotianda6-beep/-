import json

from app.services.execution_state import recent_execution_results


def test_execution_result_reason_is_localized(tmp_path, monkeypatch) -> None:
    path = tmp_path / "reverse.json"
    path.write_text(
        json.dumps({"last_result": {"status": "failed", "reason": 'bybit {"retCode":34022030,"retMsg":"Borrowing demand is high"}'}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.execution_state.STATE_FILES", (("reverse-cash-carry", "反向期现执行器", path),))

    result = recent_execution_results()[0]

    assert "借币资金池不足" in result["reason"]
    assert "Borrowing demand" not in result["reason"]
