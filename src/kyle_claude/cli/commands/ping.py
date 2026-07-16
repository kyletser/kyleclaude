from __future__ import annotations

import asyncio
import sys
import time

import kyle_claude
from kyle_claude.core.bus.commands import PongResult
from kyle_claude.core.config import KyleConfig
from kyle_claude.core.transport.auth import IpcTokenError
from kyle_claude.core.transport.socket_client import IpcError, SocketClient


# 同步入口：运行 ping 协程，连接失败时打印错误并退出
def cmd_ping(config: KyleConfig) -> None:
    try:
        asyncio.run(_ping(config))
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        sys.exit(1)
    except (IpcTokenError, IpcError) as exc:
        print(f"error: IPC authentication failed: {exc}", file=sys.stderr)
        sys.exit(1)


# 向 core 守护进程发送 ping 请求，打印 pong 响应及延迟
async def _ping(config: KyleConfig) -> None:
    t0 = time.monotonic()
    client = SocketClient.from_config(config)
    await client.connect()
    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        raw = await asyncio.wait_for(
            client.send_command(
                "core.ping",
                {"client": f"cli/{kyle_claude.__version__}"},
            ),
            timeout=10.0,
        )
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await client.close()
    latency_ms = int((time.monotonic() - t0) * 1000)
    result = PongResult.model_validate(raw)
    print(f"pong server={result.server_version} uptime={result.uptime_ms}ms latency={latency_ms}ms")
