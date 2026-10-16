"""Job queue, background execution and on-disk job records.

ENVI is a heavyweight, process-global, single-license application, so envimcp
runs **one job at a time** by default (``limits.max_concurrent = 1``). When the
single slot is busy a new submission is either queued (default) or rejected with
a pointer at the job already running, depending on ``limits.on_busy``.

Every job keeps a small JSON record in ``jobs/<id>/job.json``, which is what
makes ``envi_job_status`` work across server restarts and across the two MCP
transports (a stdio client cannot see a job started by an HTTP client unless the
record is on disk).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .runner import JobResult, Runner, RunnerError
from .scriptgen import JobPlan

__all__ = ["Job", "JobManager", "JobNotFoundError", "BusyError"]

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}

#: Failure kinds worth another attempt (``limits.max_retries``). Only
#: transport-level failures qualify: a deterministic task error (bad parameters,
#: missing input file, invalid choice) fails identically on every attempt, so
#: retrying it would just burn the single ENVI licence and bury the real message.
#: ``timeout``      - the wall clock ran out (a larger timeout may succeed)
#: ``no_status``    - IDL died or never ran, producing no status.json
#: ``busy``         - the ENVI licence was held by another process
#: ``license``      - ENVI(/HEADLESS) could not start
TRANSIENT_ERROR_KINDS = {"timeout", "no_status", "busy", "license"}


class JobNotFoundError(KeyError):
    """Raised when an unknown job id is requested."""


class BusyError(RuntimeError):
    """Raised when the single job slot is taken and ``on_busy = reject``."""

    def __init__(self, running: Job) -> None:
        self.running = running
        super().__init__(
            f"ENVI is busy with {running.id} (task={running.task!r}, status={running.status}). "
            "Wait for it with envi_job_status, or set limits.on_busy = 'queue' to wait instead."
        )


@dataclass
class Job:
    """One submitted job, in memory and on disk."""

    id: str
    plan: JobPlan
    task: str = ""
    kind: str = "TASK_WORKFLOW"
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0
    result: JobResult | None = None
    error: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    #: Set by :meth:`JobManager.cancel`; the runner kills the process tree.
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    @property
    def record_path(self) -> Path:
        return self.plan.job_dir / "job.json"

    @property
    def status_path(self) -> Path:
        return self.plan.status_path

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "job_id": self.id,
            "status": self.status,
            "kind": self.kind,
            "task": self.task,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": (self.result.duration_s if self.result else None),
            "attempts": self.attempts,
            "job_dir": str(self.plan.job_dir),
            "output_dir": str(self.plan.output_dir),
            "status_path": str(self.status_path),
            "steps": [s.to_dict() for s in self.plan.steps],
            "error": self.error,
            "params": self.params,
        }
        if self.result is not None:
            record["result"] = self.result.to_dict()
        return record


class JobManager:
    """Owns the job slot, the worker threads and the on-disk records."""

    def __init__(self, config: Config, *, reap_orphans: bool = True) -> None:
        self.config = config
        self.runner = Runner(config)
        #: After a job reaches a terminal state, look for an idl.exe still holding
        #: this job's directory and kill it. A timed-out or crashed job can
        #: otherwise keep the single ENVI licence and fail every later job.
        self.reap_orphans = reap_orphans
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._running_id: str | None = None
        self._queue: list[str] = []
        self.config.ensure_dirs()
        self._load_existing()

    # ---- persistence ----------------------------------------------------
    def _load_existing(self) -> None:
        """Re-attach to jobs left behind by an earlier server process."""
        jobs_dir = self.config.jobs_path
        if not jobs_dir.is_dir():
            return
        for record in sorted(jobs_dir.glob("*/job.json"), key=lambda p: p.stat().st_mtime):
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or not data.get("job_id"):
                continue
            job_id = str(data["job_id"])
            plan = JobPlan(
                job_id=job_id,
                kind=str(data.get("kind") or "TASK_WORKFLOW"),
                steps=(),
                job_dir=record.parent,
                output_dir=Path(str(data.get("output_dir") or record.parent / "outputs")),
                idl_lib_dir=self.config.idl_lib_path,
                batch_path=record.parent / "run_job.pro",
            )
            job = Job(
                id=job_id,
                plan=plan,
                task=str(data.get("task") or ""),
                kind=plan.kind,
                status=str(data.get("status") or "failed"),
                created_at=float(data.get("created_at") or record.stat().st_mtime),
                started_at=data.get("started_at"),
                finished_at=data.get("finished_at"),
                attempts=int(data.get("attempts") or 0),
                error=str(data.get("error") or ""),
                params=dict(data.get("params") or {}),
            )
            if job.status not in TERMINAL_STATUSES:
                # The process that owned this job is gone.
                job.status = "failed"
                job.error = job.error or "interrupted: the server that owned this job exited"
            result = data.get("result")
            if isinstance(result, dict):
                job.result = JobResult(**{k: v for k, v in result.items() if k in _RESULT_FIELDS})
            self._jobs[job_id] = job
            self._order.append(job_id)

    def _persist(self, job: Job) -> None:
        try:
            job.plan.job_dir.mkdir(parents=True, exist_ok=True)
            tmp = job.record_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(job.to_dict(), indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp.replace(job.record_path)
        except OSError:  # pragma: no cover - non-fatal
            pass

    # ---- lookups --------------------------------------------------------
    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                # Accept a server-generated prefix as long as it is unambiguous.
                matches = [j for j in self._jobs if j.startswith(job_id)]
                if len(matches) == 1:
                    job = self._jobs[matches[0]]
            if job is None:
                raise JobNotFoundError(job_id)
            if job.result is None and job.status in TERMINAL_STATUSES:
                self._refresh_from_disk(job)
            return job

    def _refresh_from_disk(self, job: Job) -> None:
        from .runner import read_status

        status = read_status(job.status_path)
        if status is None:
            return
        result = JobResult(job_id=job.id, status_path=str(job.status_path))
        Runner._apply_status(result, status)
        result.status = "succeeded" if result.ok else "failed"
        job.result = result

    def running(self) -> Job | None:
        with self._lock:
            if self._running_id is None:
                return None
            return self._jobs.get(self._running_id)

    def list(self, limit: int = 20) -> list[Job]:
        with self._lock:
            ids = list(reversed(self._order))[: max(1, limit)]
            return [self._jobs[i] for i in ids if i in self._jobs]

    # ---- submission -----------------------------------------------------
    def submit(
        self,
        plan: JobPlan,
        *,
        task: str = "",
        kind: str = "TASK_WORKFLOW",
        params: dict[str, Any] | None = None,
        background: bool = False,
        timeout_s: int | None = None,
        reaper: bool = False,
    ) -> Job:
        """Register *plan* and either run it inline or hand it to a worker thread."""
        job = Job(
            id=plan.job_id,
            plan=plan,
            task=task or (plan.steps[0].task if plan.steps else ""),
            kind=kind,
            params=dict(params or {}),
        )
        with self._condition:
            if background and self._running_id is not None:
                if self.config.limits.on_busy == "reject":
                    raise BusyError(self._jobs[self._running_id])
                job.status = "queued"
                self._queue.append(job.id)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._persist(job)

        if background:
            thread = threading.Thread(
                target=self._worker,
                args=(job, timeout_s),
                name=f"envimcp-{job.id}",
                daemon=True,
            )
            thread.start()
            return job

        self._execute(job, timeout_s)
        return job

    # ---- execution ------------------------------------------------------
    def _worker(self, job: Job, timeout_s: int | None) -> None:
        with self._condition:
            while self._running_id is not None and not job.cancel_event.is_set():
                self._condition.wait(timeout=0.5)
            if job.cancel_event.is_set():
                job.status = "canceled"
                job.finished_at = time.time()
                self._persist(job)
                return
            if job.id in self._queue:
                self._queue.remove(job.id)
        self._execute(job, timeout_s)

    def _execute(self, job: Job, timeout_s: int | None) -> None:
        with self._condition:
            self._running_id = job.id
            job.status = "running"
            job.started_at = time.time()
            self._persist(job)

        # ``limits.max_retries`` is honoured here: it is the number of ADDITIONAL
        # attempts after a failure, so the worst case is
        # timeout_s * (1 + max_retries). Only TRANSIENT failures are retried -- a
        # deterministic task error (bad parameters, missing file) would fail
        # identically and only burn the single ENVI licence while hiding the real
        # message. ``transient_errors`` records why each retry happened.
        attempts = 1 + max(0, self.config.limits.max_retries)
        result: JobResult | None = None
        transient: list[str] = []
        try:
            for attempt in range(attempts):
                if job.cancel_event.is_set():
                    break
                job.attempts = attempt + 1
                try:
                    result = self.runner.run(
                        job.plan,
                        timeout_s=timeout_s,
                        cancel_event=job.cancel_event,
                    )
                except RunnerError as exc:
                    result = JobResult(job_id=job.id, status="failed", error=str(exc))
                except Exception as exc:  # pragma: no cover - defensive
                    result = JobResult(
                        job_id=job.id,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )

                if result.ok or result.canceled:
                    break
                if result.error_kind not in TRANSIENT_ERROR_KINDS:
                    break
                transient.append(result.error_kind)
            job.result = result
            job.status = result.status if result else "failed"
            job.error = result.error if result else "the job never ran"
            if len(transient) > 0 and result is not None:
                note = f"retried after transient failure(s): {', '.join(transient)}"
                job.error = (job.error + " " if job.error else "") + note
        finally:
            job.finished_at = time.time()
            if job.attempts == 0:
                job.attempts = 1
            with self._condition:
                if self._running_id == job.id:
                    self._running_id = None
                self._persist(job)
                self._condition.notify_all()
            if self.reap_orphans and job.status in TERMINAL_STATUSES:
                # A job that timed out can leave idl.exe alive holding the single
                # ENVI licence, which would fail every later job.
                killed = self.runner.terminate_orphans(plan=job.plan)
                if killed:
                    job.error = (job.error + " " if job.error else "") + (
                        f"reaped orphan idl.exe process(es): {killed}"
                    )

    # ---- cancellation and waiting ---------------------------------------
    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self._condition:
            if job.is_terminal:
                return job
            job.cancel_event.set()
            if job.status == "queued":
                job.status = "canceled"
                job.finished_at = time.time()
                job.error = "canceled before it started"
                if job.id in self._queue:
                    self._queue.remove(job.id)
                self._persist(job)
        return job

    def wait(self, job_id: str, timeout_s: float | None = None) -> Job:
        job = self.get(job_id)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._condition:
            while not job.is_terminal:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._condition.wait(timeout=min(0.5, remaining) if remaining else None)
        return job

    def wait_for_slot(self, timeout_s: float | None = None) -> bool:
        """Block until the single job slot is free (used by synchronous tools)."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._condition:
            while self._running_id is not None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(timeout=min(0.5, remaining) if remaining else None)
            return True

    # ---- housekeeping ---------------------------------------------------
_RESULT_FIELDS = {    "job_id",
    "ok",
    "status",
    "exit_code",
    "op",
    "envi_version",
    "widget_id",
    "error",
    "error_kind",
    "elapsed_s",
    "steps",
    "outputs",
    "inspect",
    "warnings",
    "stdout",
    "stderr",
    "status_path",
    "log_path",
    "timed_out",
    "canceled",
    "duration_s",
}
