#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


# 定位待验证目录中唯一的 wheel 文件
def _find_wheel(path: Path) -> Path:
    candidates = sorted(path.glob("*.whl")) if path.is_dir() else [path]
    if len(candidates) != 1 or not candidates[0].is_file():
        raise SystemExit(f"expected exactly one wheel, found: {candidates}")
    return candidates[0].resolve()


# 校验 wheel 成员路径后安全解压到临时目录
def _safe_extract(wheel: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination):
                raise SystemExit(f"unsafe wheel member: {member.filename}")
        archive.extractall(destination)


# 向操作系统申请一个临时可用的 loopback 端口
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# 在隔离环境中执行一段 Python 代码并捕获输出
def _run_python(
    code: str,
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# 等待 wheel 中启动的 Core 开始监听或提前失败
async def _wait_until_listening(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"wheel Core exited with {process.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError("wheel Core did not listen within 10 seconds")


# 从解压后的 wheel 验证资源、入口、鉴权 Core 和真实 ping
def smoke(wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="kyle-wheel-smoke-") as raw_temp:
        root = Path(raw_temp)
        site = root / "site"
        site.mkdir()
        _safe_extract(wheel, site)

        required_resources = [
            site / "kyle_claude" / "py.typed",
            site / "kyle_claude" / "core" / "agents" / "builtin" / "executor.toml",
            site / "kyle_claude" / "core" / "skills" / "builtin" / "review.md",
        ]
        missing = [str(path.relative_to(site)) for path in required_resources if not path.is_file()]
        if missing:
            raise RuntimeError(f"wheel is missing package resources: {missing}")

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("KYLE_") and key not in {"PYTHONPATH", "ANTHROPIC_API_KEY"}
        }
        home = root / "home"
        home.mkdir()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "PYTHONPATH": str(site),
                "PYTHONUTF8": "1",
                "ANTHROPIC_API_KEY": "wheel-smoke-placeholder",
                "KYLE_HOST": "127.0.0.1",
                "KYLE_PORT": str(_free_port()),
                "KYLE_IPC_TOKEN": secrets.token_urlsafe(32),
                "KYLE_LOG_FILE": "",
                "KYLE_LOG_LEVEL": "WARNING",
                "KYLE_TRACE_ENABLED": "false",
            }
        )

        imported = _run_python(
            """
from pathlib import Path
import os
import kyle_claude
package = Path(kyle_claude.__file__).resolve()
site = Path(os.environ["PYTHONPATH"]).resolve()
assert package.is_relative_to(site), (package, site)
print(package)
""",
            env=env,
            cwd=root,
        )
        if "kyle_claude" not in imported.stdout:
            raise RuntimeError("wheel package import did not report its path")

        _run_python(
            "import sys; sys.argv=['kyle', '--version']; "
            "from kyle_claude.cli.main import main; main()",
            env=env,
            cwd=root,
        )
        _run_python(
            "import sys; sys.argv=['kyle-tui', '--help']; "
            "from kyle_claude.tui.__main__ import main; main()",
            env=env,
            cwd=root,
        )

        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from kyle_claude.core.app import run; run()",
            ],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            asyncio.run(_wait_until_listening(int(env["KYLE_PORT"]), process))
            ping = _run_python(
                "import sys; sys.argv=['kyle', 'ping']; "
                "from kyle_claude.cli.main import main; main()",
                env=env,
                cwd=root,
            )
            if not ping.stdout.startswith("pong server="):
                raise RuntimeError(f"unexpected wheel ping output: {ping.stdout!r}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)

    print(f"wheel smoke passed: {wheel.name}")


# 解析命令行参数并执行 wheel 烟测
def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test a built KyleClaude wheel")
    parser.add_argument("wheel_or_dist", type=Path)
    args = parser.parse_args()
    smoke(_find_wheel(args.wheel_or_dist))


if __name__ == "__main__":
    main()
