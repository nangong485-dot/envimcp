"""Run one generated IDL job and read back its result.

The contract with ``idl/envi_dsh_runner.pro`` is deliberately narrow:

* **stdout/stderr are diagnostics, never the answer.** IDL mixes its banner,
  compilation messages and ENVI restore chatter onto stdout, and the exit code
  is unreliable (an uncaught IDL error aborts the batch but still reports a
  status to the shell). The answer is ``status.json``.
* **The process tree must die on timeout.** ``idl.exe`` spawns ENVI and may
  leave children behind, so cancellation uses ``taskkill /T /F`` on Windows and
  a process-group signal on POSIX.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .escaping import safe_log_text
from .scriptgen import JobPlan

__all__ = [
    "JobResult",
    "Runner",
    "RunnerError",
    "read_status",
    "terminate_process_tree",
]

#: Exit codes ENVI_DSH_RUN uses, so a missing status.json is still explicable.
IDL_EXIT_MEANINGS = {
    0: "success",
    1: "IDL error before ENVI could run (see idl.log / stdout)",
    2: "ENVI(/HEADLESS) failed -- usually a licensing problem",
    3: "a task step failed or an inspection could not open the raster",
}


class RunnerError(RuntimeError):
    """Raised when a job cannot be launched at all (not when IDL fails)."""


@dataclass
class JobResult:
    """Everything known about one finished job attempt."""

    job_id: str
    ok: bool = False
    status: str = "failed"
    exit_code: int | None = None
    op: str = ""
    envi_version: str = ""
    widget_id: int | None = None
    error: str = ""
    error_kind: str = ""
    elapsed_s: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    inspect: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    status_path: str = ""
    log_path: str = ""
    timed_out: bool = False
    canceled: bool = False
    duration_s: float = 0.0
    #: The raw ``status.json`` payload, verbatim, so no field is lost in the
    #: projection onto the attributes above.
    raw_status: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "ok": self.ok,
            "status": self.status,
            "exit_code": self.exit_code,
            "op": self.op,
            "envi_version": self.envi_version,
            "widget_id": self.widget_id,
            "error": self.error,
            "error_kind": self.error_kind,
            "elapsed_s": self.elapsed_s,
            "duration_s": self.duration_s,
            "steps": self.steps,
            "outputs": self.outputs,
            "inspect": self.inspect,
            "warnings": self.warnings,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "status_path": self.status_path,
            "log_path": self.log_path,
            "timed_out": self.timed_out,
            "canceled": self.canceled,
        }

    def summary(self) -> str:
        if self.ok:
            return f"{self.job_id}: ok in {self.duration_s:.1f}s"
        return f"{self.job_id}: {self.status}" + (f" -- {self.error}" if self.error else "")


def read_status(path: Path) -> dict[str, Any] | None:
    """Read a ``status.json``; returns ``None`` when absent or unparsable."""
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def terminate_process_tree(process: subprocess.Popen[bytes], *, grace_s: float = 5.0) -> None:
    """Kill *process* and everything it spawned."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=grace_s + 5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - best effort
            pass
    else:  # pragma: no cover - this project targets Windows
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:  # pragma: no cover - already asked nicely
        try:
            process.kill()
        except OSError:
            pass


class Runner:
    """Spawns ``idl.exe -e "@<job>/run_job.pro"`` with a hard timeout."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.idl_exe = Path(config.idl.exe)

    # ---- command line ---------------------------------------------------
    def command(self, plan: JobPlan) -> list[str]:
        """``idl.exe -e "@<batch>"`` with FORWARD slashes and no added quotes.

        IDL's ``@"<file>"`` form resolves the file itself, but on this machine it
        only works when the path uses forward slashes and is not wrapped in extra
        quotes. Measured against this workspace (whose path contains a space):

        ==========================================  =======  =========  ===========
        argument passed to -e                        exit    modules    verdict
        ==========================================  =======  =========  ===========
        ``@E:\\...\\run_job.pro`` (backslash, bare)       0         0    silent no-op
        ``@"E:\\...\\run_job.pro"``                       0         0    no such file
        ``@E:/.../run_job.pro``   (forward, bare)     3        37    WORKS
        ``@"E:/.../run_job.pro"``                         0         0    no such file
        ==========================================  =======  =========  ===========

        The bare backslash form exits 0 after truncating the path at its space,
        which is why the verdict is taken from ``status.json`` and not the code.
        """
        batch = str(plan.batch_path).replace("\\", "/")
        return [str(self.idl_exe), "-e", "@" + batch]

    def environment(self, plan: JobPlan) -> dict[str, str]:
        """The environment for the IDL process: the host environment, unmodified.

        An earlier version forced ``IDL_PATH=+<IDL_DEFAULT>``; with that set, IDL
        started, printed its banner and exited immediately without executing the
        batch file at all (no compiled modules, no status.json). The stock
        environment -- the one ENVI's own launcher uses -- runs the same batch
        file correctly.
        """
        return dict(os.environ)

    def _explain_exit(self, code: int | None) -> str:
        if code is None:
            return ""
        return IDL_EXIT_MEANINGS.get(code, f"IDL exited with code {code}")

    # ---- execution ------------------------------------------------------
    def run(
        self,
        plan: JobPlan,
        *,
        timeout_s: int | None = None,
        cancel_event: Any = None,
        capture_limit: int = 8000,
    ) -> JobResult:
        """Run *plan* to completion (or timeout) and return its :class:`JobResult`."""
        plan.write()
        result = JobResult(job_id=plan.job_id, status_path=str(plan.status_path))
        result.log_path = str(plan.log_path)

        timeout = timeout_s if timeout_s is not None else self.config.limits.timeout_s
        started = time.monotonic()

        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "cwd": str(plan.job_dir),
            "env": self.environment(plan),
        }
        if sys.platform != "win32":  # pragma: no cover - Windows is the target
            popen_kwargs["start_new_session"] = True

        try:
            process = subprocess.Popen(self.command(plan), **popen_kwargs)  # noqa: S603
        except OSError as exc:
            raise RunnerError(
                f"cannot start IDL ({self.idl_exe}): {exc}. Check idl.exe in config/envimcp.toml."
            ) from exc

        timed_out = False
        canceled = False
        out: bytes | None = None
        err: bytes | None = None
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(process)
            try:
                out, err = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover - kill already failed
                out, err = b"", b""

        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            canceled = True
            if process.poll() is None:
                terminate_process_tree(process)

        result.duration_s = time.monotonic() - started
        result.stdout = safe_log_text(out, capture_limit)
        result.stderr = safe_log_text(err, capture_limit)
        result.exit_code = process.returncode
        result.timed_out = timed_out

        status = read_status(Path(result.status_path))
        if status is not None:
            self._apply_status(result, status)
        else:
            if timed_out:
                result.error = (
                    f"job timed out after {timeout}s and was killed; "
                    "raise limits.timeout_s for long tasks"
                )
                result.error_kind = "timeout"
            elif canceled:
                result.error = "job was canceled"
                result.error_kind = "canceled"
            else:
                result.error = (
                    "IDL produced no status.json. "
                    + (self._explain_exit(process.returncode) or "The process ended without a result.")
                )
                # Tag this as a transport-level failure. JobManager retries only
                # TRANSIENT_ERROR_KINDS under limits.max_retries; without this the
                # retry loop could never fire for the most common IDL failure.
                result.error_kind = "no_status"
                tail = result.stderr.strip() or result.stdout.strip()
                if tail:
                    result.error += " Last output: " + tail[-800:]

        if canceled:
            result.status = "canceled"
            result.canceled = True
        elif result.ok:
            result.status = "succeeded"
        else:
            result.status = "failed"
        return result

    @staticmethod
    def _apply_status(result: JobResult, status: dict[str, Any]) -> None:
        result.raw_status = dict(status)
        result.ok = bool(status.get("ok"))
        result.op = str(status.get("op") or "")
        result.envi_version = str(status.get("envi_version") or "")
        widget = status.get("widget_id")
        result.widget_id = widget if isinstance(widget, int) else None
        result.error = str(status.get("error") or "")
        if status.get("error_kind"):
            result.error_kind = str(status["error_kind"])
        try:
            result.elapsed_s = float(status.get("elapsed_s") or 0.0)
        except (TypeError, ValueError):
            result.elapsed_s = 0.0
        steps = status.get("step_results")
        if isinstance(steps, list):
            result.steps = [s for s in steps if isinstance(s, dict)]
        inspect = status.get("inspect")
        if isinstance(inspect, dict):
            result.inspect = inspect
        warnings = status.get("warnings")
        if isinstance(warnings, list):
            result.warnings = [str(w) for w in warnings]

        outputs: dict[str, Any] = {}
        for step in result.steps:
            step_outputs = step.get("outputs")
            if isinstance(step_outputs, dict):
                for key, value in step_outputs.items():
                    outputs.setdefault(key, value)
        result.outputs = outputs


    # ---- orphan cleanup -------------------------------------------------
    def terminate_orphans(self, *, plan: JobPlan | None = None) -> list[int]:
        """Best-effort kill of ``idl.exe`` processes this job left behind.

        A job that crashed or was killed can leave an ``idl.exe`` holding the
        single ENVI licence, which then makes every later job fail with a
        licensing error. ``taskkill /T`` on the parent normally covers this, so
        this is a safety net. Only processes whose working directory is the job
        directory are touched -- an ENVI session the user started by hand is
        never a target.

        Returns the PIDs actually killed (empty when nothing was found).
        """
        if sys.platform != "win32" or plan is None:  # pragma: no cover - Windows only
            return []
        target = str(plan.job_dir)
        pids: list[int] = []
        try:  # pragma: no cover - depends on wmic being present
            listing = subprocess.run(
                [
                    "wmic",
                    "process",
                    "where",
                    "name='idl.exe'",
                    "get",
                    "ProcessId,ExecutablePath,CommandLine",
                    "/format:csv",
                ],
                capture_output=True,
                timeout=20,
                check=False,
            )
            for line in listing.stdout.decode("utf-8", "replace").splitlines():
                if target.lower() not in line.lower():
                    continue
                tail = line.rsplit(",", 1)[-1].strip()
                if tail.isdigit():
                    pids.append(int(tail))
        except (OSError, subprocess.SubprocessError):
            return []
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):  # pragma: no cover
                pass
        return pids


def which_idl(config: Config) -> str | None:
    """Diagnostic helper: is a usable idl.exe reachable?"""
    exe = Path(config.idl.exe)
    if exe.is_file():
        return str(exe)
    found = shutil.which("idl.exe") or shutil.which("idl")
    return found
