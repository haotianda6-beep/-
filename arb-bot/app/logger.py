import logging
import re


SECRET_PATTERNS = [
    re.compile(r"(signature=)[A-Fa-f0-9]+"),
    re.compile(r"(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_-]+"),
]


def redact(text: object) -> str:
    result = str(text)
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(r"\1***", result)
    return result


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(redact(arg) for arg in record.args)
        return True


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    root = logging.getLogger()
    redacting_filter = RedactingFilter()
    root.addFilter(redacting_filter)
    for handler in root.handlers:
        handler.addFilter(redacting_filter)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def masked(value: str | None, keep: int = 4) -> str:
    if not value:
        return "missing"
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}***{value[-keep:]}"
