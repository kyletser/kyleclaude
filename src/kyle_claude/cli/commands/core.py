from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

from kyle_claude.core.config import KyleConfig
from kyle_claude.core.transport.auth import IpcTokenError
from kyle_claude.core.transport.socket_client import IpcError, SocketClient

_PID_FILE = Path.home() / ".kyle" / "kyle-core.pid"


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5  # access denied still means the PID exists
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# 尝试连接 daemon，成功则正常返回，失败则抛出 ConnectionRefusedError/OSError
async def _ping_check(config: KyleConfig) -> None:
    client = SocketClient.from_config(config)
    await client.connect()
    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        await asyncio.wait_for(
            client.send_command("core.ping", {"client": "cli/core-check"}),
            timeout=2.0,
        )
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await client.close()


async def _port_open(config: KyleConfig) -> bool:
    try:
        _reader, writer = await asyncio.open_connection(config.host, config.port)
    except (ConnectionRefusedError, OSError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


# 读取 PID 文件并确认进程存活，进程已消失则删除文件并返回 None
def _running_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        if not _pid_exists(pid):
            _PID_FILE.unlink(missing_ok=True)
            return None
        return pid
    except (ValueError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return None


# 打印 daemon 当前状态（running / not running）
def cmd_core_status(config: KyleConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"running  ({config.host}:{config.port})")
    except (ConnectionRefusedError, OSError):
        print("not running")
    except IpcTokenError as exc:
        state = "running (token unavailable)" if asyncio.run(_port_open(config)) else "not running"
        print(f"{state}  {exc}")
    except IpcError as exc:
        print(f"running (authentication failed: {exc})")


# 在后台启动 daemon，若已在运行则提示并退出
def cmd_core_start(config: KyleConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"already running  ({config.host}:{config.port})")
        return
    except (ConnectionRefusedError, OSError):
        pass
    except IpcTokenError as exc:
        if asyncio.run(_port_open(config)):
            print(f"error: core port is in use but {exc}", file=sys.stderr)
            return
    except IpcError as exc:
        print(f"error: core is running but authentication failed: {exc}", file=sys.stderr)
        return

    proc = subprocess.Popen(
        [sys.executable, "-m", "kyle_claude.core"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(proc.pid))
    print(f"started  pid={proc.pid}  ({config.host}:{config.port})")


# 向 daemon 发送 SIGTERM 停止进程，若未运行则提示
def cmd_core_stop(config: KyleConfig) -> None:
    pid = _running_pid()
    if pid is None:
        print("not running")
        return
    os.kill(pid, signal.SIGTERM)
    _PID_FILE.unlink(missing_ok=True)
    print(f"stopped  pid={pid}")
