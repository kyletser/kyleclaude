from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied" | "conflict"
    error_type: str | None = None


class ToolRetryPolicy(StrEnum):
    NEVER = "never"
    RATE_LIMIT = "rate_limit"
    IDEMPOTENT = "idempotent"


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    params_model: ClassVar[type[BaseModel] | None] = None
    retry_policy: ClassVar[ToolRetryPolicy] = ToolRetryPolicy.NEVER

    def can_retry(self, error_type: str) -> bool:
        if self.retry_policy == ToolRetryPolicy.IDEMPOTENT:
            return error_type in {"runtime_error", "rate_limited"}
        if self.retry_policy == ToolRetryPolicy.RATE_LIMIT:
            return error_type == "rate_limited"
        return False

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...
