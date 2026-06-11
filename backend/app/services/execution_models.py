from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionStep:
    name: str
    status: str = "pending"
    detail: str = ""
    raw: dict[str, Any] | None = None


@dataclass
class ExecutionResult:
    id: str
    status: str
    reason: str
    steps: list[ExecutionStep] = field(default_factory=list)
    position: Any | None = None
