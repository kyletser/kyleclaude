from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from kyle_claude.core.bus.events import StepFinishedEvent, StepStartedEvent
from kyle_claude.core.context import ExecutionContext
from kyle_claude.core.events.bus import EventBus
from kyle_claude.core.llm.base import LLMProvider
from kyle_claude.core.tools.invocation import invoke_tool
from kyle_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kyle_claude.core.compact.compactor import Compactor
    from kyle_claude.core.permissions.manager import PermissionManager


log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TranscriptSink(Protocol):
    def append_assistant(self, step: int, blocks: list[dict[str, object]]) -> None: ...

    def append_tool_result(
        self,
        step: int,
        tool_use_id: str,
        content: str,
        *,
        is_error: bool,
        block_index: int,
        block_count: int,
    ) -> None: ...


class AgentLoop:
    # 初始化循环依赖，以及可选的权限管理器、压缩器和 session ID
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
        session_id: str = "",
        transcript: TranscriptSink | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id
        self._transcript = transcript

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            # [plan] call LLM — API errors terminate the run
            try:
                response = await self._provider.chat(
                    messages=context.messages,
                    tool_schemas=self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=context.system_prompt(
                        "You are a helpful AI assistant. "
                        "Use the available tools to complete the user's goal. "
                        "Prefer glob and grep over shell commands for code discovery. "
                        "Prefer edit_file over write_file when changing an existing file. "
                        "Use apply_patch for related changes across multiple files. "
                        "File changes are checkpointed automatically; use checkpoint_rewind "
                        "when the user asks to undo the latest agent change. "
                        "When the goal is fully achieved, respond with a final answer "
                        "and do not call any more tools."
                    ),
                )
            except asyncio.CancelledError:
                context.mark_failed("cancelled")
                raise
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                break

            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            context.add_assistant_message(blocks)
            if self._transcript is not None:
                self._transcript.append_assistant(context.step, blocks)

            # [act] execute each requested tool; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                for result_index, tc in enumerate(response.tool_calls):
                    result = await invoke_tool(
                        self._registry, tc, self._bus, context.run_id,
                        permission_manager=self._permission_manager,
                        session_id=self._session_id,
                    )
                    context.add_tool_result(tc.id, result.content, is_error=result.is_error)
                    if self._transcript is not None:
                        self._transcript.append_tool_result(
                            context.step,
                            tc.id,
                            result.content,
                            is_error=result.is_error,
                            block_index=result_index,
                            block_count=len(response.tool_calls),
                        )
                    if result.error_type == "permission_required":
                        context.mark_failed("permission_required")
                        break
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for result_index, tc in enumerate(response.tool_calls):
                    error = (
                        "Error: output token limit reached before this tool call could be "
                        "completed. Please break the task into smaller steps and try again."
                    )
                    context.add_tool_result(tc.id, error, is_error=True)
                    if self._transcript is not None:
                        self._transcript.append_tool_result(
                            context.step,
                            tc.id,
                            error,
                            is_error=True,
                            block_index=result_index,
                            block_count=len(response.tool_calls),
                        )

            # Termination check — end_turn wins over max_steps if both hit on same step
            if response.stop_reason == "end_turn":
                context.result = response.text or ""
                context.mark_success()
            elif not context.is_done() and context.step >= context.max_steps:
                context.mark_failed("exceeded_max_steps")

            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not context.is_done()
                and response.stop_reason == "tool_use"
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                await self._compactor.compact(context, self._provider)

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )
