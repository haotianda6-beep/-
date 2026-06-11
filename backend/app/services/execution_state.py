import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]

STATE_FILES = (
    ("cash-carry", "正向期现执行器", PROJECT_ROOT / "config" / "cash_carry_execution_state.json"),
)


def recent_execution_results() -> list[dict[str, str]]:
    results = []
    for strategy_id, title, path in STATE_FILES:
        result = _last_result(path)
        if not result:
            continue
        results.append(
            {
                "strategy_id": strategy_id,
                "title": title,
                "status": str(result.get("status", "")),
                "reason": _localized_reason(str(result.get("reason", ""))),
                "at": str(result.get("at", "")),
            }
        )
    return results


def _last_result(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    result = data.get("last_result")
    return result if isinstance(result, dict) else None


def _localized_reason(reason: str) -> str:
    text = reason[:500]
    lower = text.lower()
    if "fromtypespot.wallet.fromtype.empty" in lower or "totypespot.wallet.totype.empty" in lower:
        return "BITGET 划转账户类型为空；系统已改为使用 Bitget 支持的账户类型。若仍失败，请确认对应账户 USDT 可划转。"
    if "maximum transferable amount" in lower:
        return "BITGET 现货账户可划转 USDT 不足；系统已改为反向开仓前先从合约账户补到现货，再划转到跨保证金。若仍失败，请确认合约账户可划转余额。"
    if "fromaccount can not be toaccount" in lower:
        return "统一账户无需重复划转，系统会跳过该划转步骤。"
    if "insufficient" in lower or "not enough" in lower or "余额不足" in text:
        return "账户可用余额不足，请检查对应交易所现货、合约或保证金账户资金。"
    return text[:220]
