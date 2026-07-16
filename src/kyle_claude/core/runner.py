from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kyle_claude.core.bus.events import RunFinishedEvent, RunStartedEvent
from kyle_claude.core.checkpoints import CheckpointStore
from kyle_claude.core.compact.compactor import Compactor
from kyle_claude.core.config import KyleConfig
from kyle_claude.core.context import ExecutionContext
from kyle_claude.core.events.bus import EventBus, EventHandler
from kyle_claude.core.events.writer import EventWriter
from kyle_claude.core.llm.base import LLMProvider
from kyle_claude.core.llm.factory import create_llm_provider
from kyle_claude.core.loop import AgentLoop
from kyle_claude.core.mcp.server import McpServerManager
from kyle_claude.core.memory.loader import load_context_file
from kyle_claude.core.permissions.manager import PermissionManager
from kyle_claude.core.runs import RUNS_DIR, new_run_id
from kyle_claude.core.session.model import Session
from kyle_claude.core.session.store import SessionStore, SessionTranscriptSink
from kyle_claude.core.subagent.registry import BackgroundTaskRegistry
from kyle_claude.core.subagent.tool import AgentResultTool, SpawnAgentTool
from kyle_claude.core.task.manager import TaskManager
from kyle_claude.core.tools.builtin import (
    ApplyPatchTool,
    BashTool,
    CheckpointListTool,
    CheckpointRewindTool,
    EditFileTool,
    GitDiffTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    NoteSaveTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
)
from kyle_claude.core.tools.registry import ToolRegistry
from kyle_claude.core.trace.provider import TracingProvider
from kyle_claude.core.trace.writer import TraceWriter
from kyle_claude.core.workspace import WorkspaceBoundary


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: KyleConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        self._workspace_boundary = WorkspaceBoundary.current()
        # 跨 run 共享的后台 subagent 任务注册表
        self._task_registry = BackgroundTaskRegistry()

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）；可选注入 SpawnAgentTool
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist else None

        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        for t in [
            ReadFileTool(self._workspace_boundary),
            GlobTool(self._workspace_boundary),
            GrepTool(self._workspace_boundary),
            GitDiffTool(self._workspace_boundary),
            BashTool(),
            EditFileTool(
                self._workspace_boundary,
                checkpoint_store=checkpoint_store,
            ),
            ApplyPatchTool(
                self._workspace_boundary,
                checkpoint_store=checkpoint_store,
            ),
            WriteFileTool(
                self._workspace_boundary,
                checkpoint_store=checkpoint_store,
            ),
            ListDirTool(self._workspace_boundary),
        ]:
            if _ok(t.name):
                registry.register(t)
        if checkpoint_store is not None:
            for checkpoint_tool in [
                CheckpointListTool(checkpoint_store),
                CheckpointRewindTool(checkpoint_store),
            ]:
                if _ok(checkpoint_tool.name):
                    registry.register(checkpoint_tool)
        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _ok(t.name):
                registry.register(t)
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(store, session.id, run_id)
            if _ok(note_tool.name):
                registry.register(note_tool)
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            if _ok("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=self._config.agent.max_steps,
                        task_registry=self._task_registry,
                        runs_dir=runs_dir,
                        session_id=session_id,
                        depth=0,
                        workspace_boundary=self._workspace_boundary,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry))
        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        return registry

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        global_ctx = load_context_file(Path("~/.kyle/context.md").expanduser())
        project_ctx = load_context_file(Path(".kyle/context.md"))

        task_manager = TaskManager(run_path / ".tasks")
        checkpoint_store = CheckpointStore(
            run_path / ".checkpoints",
            self._workspace_boundary,
        )

        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            project_context=project_ctx,
            system_prompt_override=system_prompt_override,
        )
        transcript = (
            SessionTranscriptSink(store, session.id, run_id)
            if session is not None and store is not None
            else None
        )

        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            cancelled = False
            try:
                provider: LLMProvider = self._provider or create_llm_provider(self._config.llm)
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                session_id_str = session.id if session is not None else ""
                child_runs_dir = (
                    store.runs_dir(session.id)
                    if session is not None and store is not None
                    else self._runs_dir
                )
                registry = self._build_registry(
                    task_manager,
                    session=session,
                    store=store,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    child_runs_dir=child_runs_dir,
                    session_id=session_id_str,
                    tool_whitelist=tool_whitelist,
                    checkpoint_store=checkpoint_store,
                )
                session_dir = (
                    store.session_dir(session.id)
                    if session is not None and store is not None
                    else run_path
                )
                compactor = Compactor(
                    bus,
                    session_dir,
                    session_id_str,
                    store=store if session is not None else None,
                )
                loop = AgentLoop(
                    provider, registry, bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=session_id_str,
                    transcript=transcript,
                )
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")
            except Exception:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("llm_error")

            # A Runner is scoped to one turn, so no background child may outlive it.
            await asyncio.shield(self._task_registry.cancel_descendants(run_id))
            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if session is not None and store is not None:
            store.recover_incomplete_tail(session.id)

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )
