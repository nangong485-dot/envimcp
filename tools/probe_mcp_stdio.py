#!/usr/bin/env python3
"""Minimal MCP stdio handshake probe — no dependencies, no MCP SDK.

Answers one question: *does this exact command line behave like an MCP server?*
It launches the command, performs the stdio handshake, and reports the server's
identity, tool list and resource list. Use it to validate a client configuration
before blaming the client, and to see the real error when an MCP host silently
refuses to start a server.

    python tools/probe_mcp_stdio.py -- <command> [args ...]

For example, for this project::

    .venv\\Scripts\\python.exe tools\\probe_mcp_stdio.py -- \\
        .venv\\Scripts\\python.exe -m envimcp.cli --config config\\envimcp.toml \\
        serve --transport stdio

The child's stderr is inherited, so server log lines appear in the same terminal
— they are usually the actual diagnosis when the handshake fails.

Exit status: 0 when initialize + tools/list succeeded, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

# Newest first; the first version the server accepts is used. Servers that know
# only an older revision answer with an error and we fall back.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

DEFAULT_TIMEOUT = 30.0


def _short(value: object, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class StdioClient:
    """Tiny JSON-RPC 2.0 client speaking newline-delimited JSON over stdio."""

    def __init__(self, argv: list[str], timeout: float, env: dict[str, str] | None = None):
        self.timeout = timeout
        self._inbox: "queue.Queue[dict | None]" = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # inherit: server logs are part of the diagnosis
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, **(env or {})},
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()

    # -- plumbing ---------------------------------------------------------
    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[probe] ignoring non-JSON line on stdout: {_short(line, 200)}", file=sys.stderr)
                    continue
                self._inbox.put(message)
        finally:
            self._inbox.put(None)  # EOF marker

    def _write(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        payload: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method}: no reply within {self.timeout:g}s")
            try:
                message = self._inbox.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"{method}: no reply within {self.timeout:g}s") from None
            if message is None:
                raise ConnectionError(f"server closed stdout while waiting for {method!r}")
            if message.get("id") != request_id:
                continue  # a notification or an unrelated reply
            if "error" in message:
                raise RuntimeError(f"{method} failed: {_short(message['error'])}")
            return message.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    # -- shutdown ---------------------------------------------------------
    def close(self) -> None:
        """Close stdin so the server can exit, then make sure it really did.

        A Windows ``venv`` interpreter is a launcher that re-executes the base
        interpreter, so terminating only the direct child would orphan the
        process that actually runs the server; kill the whole tree instead.
        """
        if self.proc.poll() is None and self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"[probe] warning: could not stop pid {self.proc.pid}", file=sys.stderr)


def initialize(client: StdioClient, client_name: str) -> dict:
    """Negotiate a protocol revision, trying each known version in turn."""
    last_error: Exception | None = None
    for version in PROTOCOL_VERSIONS:
        try:
            result = client.request(
                "initialize",
                {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": client_name, "version": "1.0"},
                },
            )
        except RuntimeError as exc:  # server refused this revision
            last_error = exc
            continue
        client.notify("notifications/initialized")
        return result
    raise RuntimeError(f"initialize rejected for every known protocol version ({last_error})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_mcp_stdio.py",
        description="Perform an MCP stdio handshake against a server command.",
        epilog="Everything after `--` is the server command line.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds to wait for each reply (default: %(default)s)")
    parser.add_argument("--call", metavar="TOOL", help="after listing, call this tool")
    parser.add_argument("--call-args", metavar="JSON", default="{}", help="arguments object for --call (default: {})")
    parser.add_argument("--quiet", action="store_true", help="print only the summary line")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="server command line, after `--`")
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no server command given; put it after `--`")

    def say(*parts: object) -> None:
        if not args.quiet:
            print(*parts)

    say(f"[probe] command: {' '.join(command)}")
    client = StdioClient(command, timeout=args.timeout)
    try:
        result = initialize(client, os.path.basename(__file__))
        server = result.get("serverInfo", {})
        say(f"[probe] serverInfo    : {_short(server)}")
        say(f"[probe] protocol      : {result.get('protocolVersion')}")
        capabilities = result.get("capabilities", {})
        say(f"[probe] capabilities  : {', '.join(sorted(capabilities)) or '(none)'}")

        tools = client.request("tools/list").get("tools", [])
        say(f"[probe] tools ({len(tools)})     : {', '.join(t.get('name', '?') for t in tools) or '(none)'}")

        resources: list[dict] = []
        if "resources" in capabilities:
            resources = client.request("resources/list").get("resources", [])
            say(f"[probe] resources ({len(resources)}): {', '.join(r.get('uri', '?') for r in resources) or '(none)'}")
            try:
                templates = client.request("resources/templates/list").get("resourceTemplates", [])
            except (RuntimeError, TimeoutError):
                templates = []
            say(f"[probe] templates ({len(templates)}): {', '.join(t.get('uriTemplate', '?') for t in templates) or '(none)'}")

        if args.call:
            call_args = json.loads(args.call_args)
            say(f"[probe] calling {args.call}({_short(call_args, 120)})")
            call_result = client.request("tools/call", {"name": args.call, "arguments": call_args})
            say(f"[probe] result        : {_short(call_result)}")

        print(
            f"[probe] OK - {server.get('name', 'server')} "
            f"{server.get('version', '')}".rstrip()
            + f", {len(tools)} tools, {len(resources)} resources"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        print(f"[probe] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
