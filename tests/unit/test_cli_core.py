from __future__ import annotations

import os

from kyle_claude.cli.commands.core import _pid_exists


def test_pid_exists_detects_current_process_and_missing_pid() -> None:
    assert _pid_exists(os.getpid())
    assert not _pid_exists(2_147_483_647)
