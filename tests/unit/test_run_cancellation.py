from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from kyle_claude.core.context import ExecutionContext
from kyle_claude.core.subagent.registry import BackgroundTaskRegistry
from kyle_claude.core.tools.builtin.bash import BashTool


async def test_bash_cancellation_terminates_descendant_process(tmp_path: Path) -> None:
    script = tmp_path / "parent.py"
    child_started = tmp_path / "child-started.txt"
    child_survived = tmp_path / "child-survived.txt"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "started, survived = sys.argv[1], sys.argv[2]\n"
        "code = \"import pathlib, sys, time; time.sleep(2); "
        "pathlib.Path(sys.argv[1]).write_text('alive')\"\n"
        "child = subprocess.Popen([sys.executable, '-c', code, survived])\n"
        "pathlib.Path(started).write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}" "{child_started}" "{child_survived}"'
    task = asyncio.create_task(BashTool().invoke({"command": command, "timeout": 120}))

    async with asyncio.timeout(5):
        while not child_started.exists():
            await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(2.5)
    assert not child_survived.exists()


async def test_background_registry_cancels_only_run_descendants() -> None:
    registry = BackgroundTaskRegistry()
    release = asyncio.Event()

    async def wait_forever() -> None:
        await release.wait()

    child_context = ExecutionContext("child", "child", 1)
    grandchild_context = ExecutionContext("grandchild", "grandchild", 1)
    unrelated_context = ExecutionContext("unrelated", "unrelated", 1)
    child_task = asyncio.create_task(wait_forever())
    grandchild_task = asyncio.create_task(wait_forever())
    unrelated_task = asyncio.create_task(wait_forever())
    registry.register("child", child_task, child_context, parent_run_id="root")
    registry.register(
        "grandchild",
        grandchild_task,
        grandchild_context,
        parent_run_id="child",
    )
    registry.register(
        "unrelated",
        unrelated_task,
        unrelated_context,
        parent_run_id="other-root",
    )

    await registry.cancel_descendants("root")

    assert child_task.cancelled()
    assert grandchild_task.cancelled()
    assert child_context.reason == "cancelled"
    assert grandchild_context.reason == "cancelled"
    assert not unrelated_task.done()
    await registry.cancel_all()
    assert unrelated_task.cancelled()
