"""Scratch smoke: real MCP handshakes over both transports.

Starts `envimcp serve` as a child process, connects with the mcp SDK's own
client, and reports what the client sees. This is AC5's "both transports work"
check done the honest way -- through the protocol, not by calling the server
object's methods in-process.
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # <root>/tools/<script>.py
PY = ROOT / ".venv" / "Scripts" / "python.exe"
CONFIG = ROOT / "config" / "envimcp.toml"
PORT = 8765


def wait_for_port(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


async def over_http() -> dict:
    from mcp import Client

    def unpack(result, *attrs):
        """mcp 2.x returns a pydantic result object; older code returned a tuple."""
        if isinstance(result, tuple):
            result = result[0]
        for attr in attrs:
            inner = getattr(result, attr, None)
            if inner is not None:
                return list(inner)
        return list(result)

    def field(items, attr):
        values = []
        for item in items:
            while isinstance(item, tuple):
                item = item[0]
            values.append(str(getattr(item, attr, item)))
        return values

    async with Client(f"http://127.0.0.1:{PORT}/mcp") as client:
        tools = field(unpack(await client.list_tools(), "tools"), "name")
        resources = field(unpack(await client.list_resources(), "resources"), "uri")
        templates = field(
            unpack(await client.list_resource_templates(), "resource_templates"),
            "uri_template",
        )
        tasks_resource = await client.read_resource("envi://tasks")
        head = ""
        blocks = getattr(tasks_resource, "contents", None) or tasks_resource
        if blocks:
            head = str(getattr(blocks[0], "text", blocks[0]))[:120]
        return {
            "tools": sorted(tools),
            "resources": sorted(resources),
            "templates": sorted(templates),
            "tasks_resource_head": head,
        }


async def over_stdio() -> dict:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=str(PY),
        args=["-m", "envimcp.cli", "--config", str(CONFIG), "serve", "--transport", "stdio"],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return {"tools": sorted(t.name for t in tools.tools)}


def main() -> int:
    print("=== stdio transport ===")
    try:
        stdio = asyncio.run(asyncio.wait_for(over_stdio(), timeout=60))
        print(json.dumps(stdio, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"stdio FAILED: {type(exc).__name__}: {exc}")

    print("=== http transport ===")
    log = ROOT / "_http_server.log"
    server = subprocess.Popen(  # noqa: S603
        [str(PY), "-m", "envimcp.cli", "--config", str(CONFIG), "serve",
         "--transport", "http", "--port", str(PORT)],
        cwd=str(ROOT),
        stdout=log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for_port(PORT):
            print("server never opened the port; log:")
            print(log.read_text(encoding="utf-8", errors="replace")[:2000])
            return 1
        http = asyncio.run(asyncio.wait_for(over_http(), timeout=60))
        print(json.dumps(http, indent=2))
    except BaseException as exc:  # noqa: BLE001
        import traceback

        for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
            print(line.rstrip())

        def unwrap(err, depth=0):
            subs = getattr(err, "exceptions", None)
            if subs:
                for sub in subs:
                    unwrap(sub, depth + 1)
            else:
                print(f"  {'  ' * depth}LEAF: {type(err).__name__}: {err}")

        unwrap(exc)
        print("--- server log ---")
        print(log.read_text(encoding="utf-8", errors="replace")[:2000])
        return 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
