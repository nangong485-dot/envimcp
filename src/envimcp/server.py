"""The MCP server: eight tools and four resources over one shared job manager.

Design notes worth keeping in mind when editing this file:

* **The tool count is deliberately small and fixed.** ENVI 5.2 ships 58 tasks;
  registering one tool per task would flood every client's context with 58
  schemas. Instead ``envi_get_task_schema`` returns a task's schema on demand and
  the ``envi://tasks/<name>`` resources carry the same information for clients
  that browse resources.
* **Verdicts come from ``status.json``, never from IDL's exit code.** IDL exits 0
  after silently truncating a bad ``-e`` path and 3 after a task failure, so the
  runner reads the file. That is why :class:`~envimcp.runner.JobResult` keeps both
  ``raw_status`` and projected attributes.
* **ENVI is single-licensed and process-global**, so one job runs at a time
  (``limits.max_concurrent``); a second request is queued or rejected according
  to ``limits.on_busy``. Every tool that starts work returns a ``job_id`` even
  when it also waits, so the client can always ask again.
* ``envi_run_idl`` is registered only when ``security.allow_raw_idl`` is set: it
  executes arbitrary IDL source with the full privileges of the ENVI session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .catalog import UnknownTaskError, cached_catalog
from .config import Config, load_config
from .jobs import BusyError, Job, JobManager, JobNotFoundError
from .raster import RasterError, cog_backend, compute_stats, raster_info
from .schema import ValidationError, task_schema
from .scriptgen import (
    EXPORT_FORMATS,
    PlannedStep,
    ScriptGenError,
    plan_export,
    plan_inspect,
    plan_workflow,
)

__all__ = ["EnvimcpServer", "build_server", "run_server"]

#: How long a synchronous tool waits for the single ENVI slot before giving up.
#: The job still runs; the caller is told to poll ``envi_job_status``.
SYNC_WAIT_SLACK_S = 30.0


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


class EnvimcpServer:
    """Holds the shared state (config, catalog, job manager) for all tools."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        self.catalog = cached_catalog(self.config.envi.task_template_dir)
        self.jobs = JobManager(self.config)
        self.config.ensure_dirs()

    # ---- helpers --------------------------------------------------------
    def describe_environment(self) -> dict[str, Any]:
        info = self.config.describe()
        info["task_count"] = len(self.catalog)
        info["catalog_failures"] = list(self.catalog.load_failures)
        info["cog_backend"] = cog_backend()
        info["problems"] = self.config.validate()
        info["idle"] = self.jobs.running() is None
        return info

    def _job_payload(self, job: Job, *, wait_s: float = 0.0) -> dict[str, Any]:
        """One uniform job envelope for every tool that starts or reports work."""
        if wait_s > 0 and not job.is_terminal:
            job = self.jobs.wait(job.id, timeout_s=wait_s)
        payload: dict[str, Any] = {
            "job_id": job.id,
            "status": job.status,
            "kind": job.kind,
            "task": job.task,
            "running": not job.is_terminal,
            "job_dir": str(job.plan.job_dir),
            "status_path": str(job.status_path),
        }
        if job.result is not None:
            result = job.result
            payload.update(
                {
                    "ok": result.ok,
                    "exit_code": result.exit_code,
                    "envi_version": result.envi_version,
                    "elapsed_s": result.elapsed_s,
                    "duration_s": result.duration_s,
                }
            )
            if result.error:
                payload["error"] = result.error
                payload["error_kind"] = result.error_kind
            if result.outputs:
                payload["outputs"] = result.outputs
            if result.steps:
                payload["steps"] = result.steps
            if result.raw_status.get("inspect"):
                payload["inspect"] = result.raw_status["inspect"]
            if result.timed_out:
                payload["timed_out"] = True
            if result.canceled:
                payload["canceled"] = True
            if result.warnings:
                payload["warnings"] = result.warnings
            if result.stdout:
                payload["stdout_tail"] = result.stdout[-2000:]
        if not job.is_terminal:
            payload["next_step"] = (
                f"Call envi_job_status with job_id={job.id!r} to follow it; "
                f"envi_job_cancel to stop it."
            )
        return payload

    def _run_plan(self, plan, *, wait_s: float, timeout_s: int | None) -> dict[str, Any]:
        running = self.jobs.running()
        if running is not None and self.config.limits.on_busy == "reject":
            raise BusyError(running)
        job = self.jobs.submit(
            plan,
            task=plan.steps[0].task if plan.steps else plan.kind,
            kind=plan.kind,
            background=True,
            timeout_s=timeout_s,
        )
        return self._job_payload(job, wait_s=wait_s)

    def _task_spec(self, name: str):
        return self.catalog.resolve(name, strict=True)

    # ---- tools ----------------------------------------------------------
    def envi_list_tasks(self, query: str = "", limit: int = 80) -> dict[str, Any]:
        """List or search the ENVITask operators this installation provides.

        Args:
            query: Free text matched against task name, display name and
                description (case-insensitive). Empty lists everything.
            limit: Maximum number of tasks to return.

        Returns:
            The matching task names with their display name and one-line
            description, plus the total available.
        """
        specs = self.catalog.search(query) if query else list(self.catalog)
        capped = max(1, int(limit))
        return {
            "total_available": len(self.catalog),
            "matched": len(specs),
            "returned": min(len(specs), capped),
            "tasks": [
                {
                    "name": spec.name,
                    "display_name": spec.display_name,
                    "description": spec.description,
                    "required_inputs": [p.name for p in spec.required_inputs],
                    "outputs": [p.name for p in spec.outputs],
                }
                for spec in specs[:capped]
            ],
        }

    def envi_get_task_schema(self, name: str) -> dict[str, Any]:
        """Return the full JSON Schema for one ENVITask, ready to build a call.

        Args:
            name: Task name as listed by ``envi_list_tasks`` (e.g.
                ``SpectralIndex``). Alternate spellings are accepted.

        Returns:
            The schema plus the accepted choice values for enumerated
            parameters, so no guessing is needed for e.g. ``INDEX``.
        """
        spec = self._task_spec(name)
        schema = task_schema(spec)
        choices = {
            p.name: list(p.choices)
            for p in spec.parameters
            if p.has_choice_list and p.choices
        }
        return {
            "task": spec.name,
            "display_name": spec.display_name,
            "description": spec.description,
            "source": spec.source_path,
            "schema": schema,
            "choices": choices,
            "output_parameter": schema.get("x-envi-output-parameter"),
            "options": [p.to_dict() for p in spec.optional_inputs],
        }

    def envi_run_task(
        self,
        task: str,
        inputs: dict[str, Any] | None = None,
        output: str | None = None,
        timeout_s: int = 0,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Run one ENVITask and return its output raster metadata and statistics.

        Args:
            task: Task name, e.g. ``SpectralIndex``.
            inputs: Parameter object exactly as ``envi_get_task_schema``
                describes it (e.g. ``{"INPUT_RASTER": "...", "INDEX": "NDVI"}``).
            output: Where to write the primary raster. Use ``#name`` for
                ``outputs/name.dat``, ``!name`` for the job directory, or an
                absolute path. Defaults to ``!name.dat`` inside the job.
            timeout_s: Wall-clock budget for the job; 0 uses the configured
                default (``limits.timeout_s``).
            wait: When true, wait for the job to finish (bounded) and return its
                result. When false, return immediately with a ``job_id``.

        Returns:
            The job envelope: ``job_id``, ``status``, and on success the output
            paths, raster geometry and per-band statistics.
        """
        spec = self._task_spec(task)
        destination = output or OUTPUT_RASTER
        primary = spec.output_for()
        if primary is None:
            raise ValidationError(
                f"{spec.name} declares no output parameter, so it cannot be run through "
                "envi_run_task",
                [f"{spec.name}: no output parameter"],
            )
        if not destination:
            destination = "!" + spec.name.lower() + ".dat"
        workflow = plan_workflow(
            catalog=self.catalog,
            config=self.config,
            steps=[
                PlannedStep(
                    spec.name,
                    inputs=dict(inputs or {}),
                    outputs={primary.name: destination},
                )
            ],
            kind="TASK_EXECUTE",
        )
        return self._run_plan(
            workflow.plan,
            wait_s=float(timeout_s or self.config.limits.timeout_s) + SYNC_WAIT_SLACK_S
            if wait
            else 0.0,
            timeout_s=int(timeout_s) or None,
        )

    def envi_create_task_workflow(
        self,
        steps: list[dict[str, Any]],
        timeout_s: int = 0,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Run several ENVITasks in sequence, wiring them together automatically.

        Args:
            steps: Ordered list of ``{"task": ..., "inputs": {...}, "outputs":
                {...}}`` objects. A raster input written as ``*OUTPUT_RASTER``
                takes the raster produced by an earlier step without writing an
                intermediate file, so a chain such as radiometric calibration ->
                QUAC -> NDVI costs no disk I/O between steps. The final step must
                write a real file (``!name`` or ``#name``).
            timeout_s: Wall-clock budget for the whole chain; 0 uses the default.
            wait: Wait for completion instead of returning a ``job_id`` only.

        Returns:
            The job envelope, including a per-step record for every stage.
        """
        if not steps:
            raise ScriptGenError("a workflow needs at least one step")
        workflow = plan_workflow(
            catalog=self.catalog,
            config=self.config,
            steps=[PlannedStep(**dict(step)) for step in steps],
            kind="TASK_WORKFLOW",
        )
        return self._run_plan(
            workflow.plan,
            wait_s=float(timeout_s or self.config.limits.timeout_s) + SYNC_WAIT_SLACK_S
            if wait
            else 0.0,
            timeout_s=int(timeout_s) or None,
        )

    def envi_inspect_raster(
        self,
        path: str,
        stats: bool = True,
        use_envi: bool = False,
    ) -> dict[str, Any]:
        """Describe a raster: geometry, data type, projection and statistics.

        Args:
            path: ENVI dataset basename (``D:/data/img``), ``.dat``/``.tif``
                file, or a ``.hdr``.
            stats: Include min/max/mean/stddev per band.
            use_envi: Ask ENVI instead of reading the file directly. The direct
                reader needs no licence and no job slot and is the default; set
                this when the dataset is not a plain ENVI file.

        Returns:
            The raster description. With ``use_envi`` the answer arrives with a
            ``job_id`` and an ``inspect`` object.
        """
        if use_envi:
            plan = plan_inspect(config=self.config, raster=path, stats=stats)
            return self._run_plan(plan, wait_s=SYNC_WAIT_SLACK_S + 300.0, timeout_s=None)
        try:
            info = raster_info(path)
        except RasterError as exc:
            raise RasterError(
                f"{exc} -- pass use_envi=True to let ENVI open it instead"
            ) from exc
        payload: dict[str, Any] = {"raster": info.to_dict(), "source": "direct-read"}
        if stats:
            payload["stats"] = compute_stats(path)
        return payload

    def envi_export_raster(
        self,
        path: str,
        destination: str,
        format: str = "tiff",  # noqa: A002 - "format" is the ENVI wording
        wait: bool = True,
    ) -> dict[str, Any]:
        """Write a raster in another format (GeoTIFF or ENVI).

        Args:
            path: Raster to read, as for ``envi_inspect_raster``.
            destination: Output path; ``#name`` writes to ``outputs/``.
            format: ``tiff`` (default, GeoTIFF) or ``envi``. ENVI 5.2 has no
                Cloud-Optimized-GeoTIFF writer, so ``cog`` is refused here; for a
                real COG, install the optional ``[geo]`` extra and use GDAL.
            wait: Wait for completion instead of returning a ``job_id`` only.

        Returns:
            The job envelope, with the written path under ``inspect.export_path``.
        """
        plan = plan_export(
            config=self.config, raster=path, destination=destination, fmt=format
        )
        return self._run_plan(
            plan,
            wait_s=SYNC_WAIT_SLACK_S + 300.0 if wait else 0.0,
            timeout_s=None,
        )

    def envi_job_status(self, job_id: str) -> dict[str, Any]:
        """Report one job: status, result, outputs and a tail of IDL output.

        Args:
            job_id: Id returned by any tool that starts work. A unique prefix is
                accepted.

        Returns:
            The same job envelope the starting tool returned, refreshed.
        """
        return self._job_payload(self.jobs.get(job_id))

    def envi_job_cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued or running job and kill its IDL process tree.

        Args:
            job_id: Id returned by any tool that starts work.

        Returns:
            The job envelope after cancellation.
        """
        return self._job_payload(self.jobs.cancel(job_id))

    def envi_run_idl(self, source: str, timeout_s: int = 300) -> dict[str, Any]:
        """Escape hatch: run arbitrary IDL source in a headless ENVI session.

        Only registered when ``security.allow_raw_idl`` is true, because this
        executes whatever it is given with full ENVI privileges. Prefer a task.

        Args:
            source: IDL statements to execute. ``ENVI(/HEADLESS)`` is already
                started; finish with ``e.Close``.
            timeout_s: Wall-clock budget.

        Returns:
            The job envelope, with IDL's own output under ``stdout_tail``.
        """
        if not self.config.security.allow_raw_idl:
            raise PermissionError(
                "envi_run_idl is disabled. Set security.allow_raw_idl = true in "
                "config/envimcp.toml to enable this escape hatch."
            )
        plan = self._raw_idl_plan(source)
        return self._run_plan(plan, wait_s=float(timeout_s) + SYNC_WAIT_SLACK_S, timeout_s=timeout_s)

    def _raw_idl_plan(self, source: str):
        """Wrap free-form IDL in the same job shape as everything else."""
        from .scriptgen import JobPlan, job_id

        jid = job_id("idlsrc")
        jdir = self.config.jobs_path / jid
        body = "\n".join(
            [
                "PRO ENVI_DSH_RAW_MAIN",
                "  COMPILE_OPT IDL2",
                *_raw_common_block(),
                "  appStarted = 0B",
                "  failed = 0B",
                "  failure = ''",
                "  startupErr = ''",
                "  enviVersion = ''",
                "  errCount = 0L",
                "  rasterCount = 0L",
                "  rasters = OBJARR(64)",
                "  rasterNames = STRARR(64)",
                "  jobId = 'raw'",
                "  opName = 'RAW_IDL'",
                "  inspectJson = ''",
                f"  statusFile = '{self._p(jdir / 'status.json')}'",
                f"  logFile = '{self._p(jdir / 'idl.log')}'",
                f"  progressFile = '{self._p(jdir / 'progress.jsonl')}'",
                f"  outDir = '{self._p(jdir / 'outputs')}'",
                "  ENVI_DSH_MAKEDIR, outDir",
                "  ENVI_DSH_START, OK = started, ERRMSG = startMsg",
                "  IF ~started THEN BEGIN",
                "    failed = 1B",
                "    failure = startMsg",
                "  ENDIF ELSE BEGIN",
                "    CATCH, cErr",
                "    IF cErr NE 0 THEN BEGIN",
                "      em = !ERROR_STATE.MSG",
                "      CATCH, /CANCEL",
                "      failed = 1B",
                "      failure = 'raw IDL failed: ' + em",
                "    ENDIF ELSE BEGIN",
                *[f"      {line}" for line in source.splitlines()],
                "      CATCH, /CANCEL",
                "    ENDELSE",
                "  ENDELSE",
                "  IF OBJ_VALID(e) THEN e.Close",
                "  IF failed THEN EXIT, STATUS = 3",
                "  EXIT, STATUS = 0",
                "END",
            ]
        )
        plan = _RawIdlPlan(
            job_id=jid,
            kind="RAW_IDL",
            steps=(),
            job_dir=jdir,
            output_dir=jdir / "outputs",
            idl_lib_dir=self.config.idl_lib_path,
            batch_path=jdir / "run_raw.pro",
            config=self.config,
            extra_body=body,
        )
        return plan

    @staticmethod
    def _p(path: Path) -> str:
        return str(path).replace("\\", "/")

    # ---- resources ------------------------------------------------------
    def resource_tasks(self) -> str:
        """The whole catalog: every task name, display name and description."""
        return _json(
            {
                "template_dir": self.catalog.template_dir,
                "count": len(self.catalog),
                "tasks": [spec.to_dict() for spec in self.catalog],
            }
        )

    def resource_task(self, name: str) -> str:
        """One task in full: its schema, accepted choices and source file."""
        return _json(self.envi_get_task_schema(name))

    def resource_config(self) -> str:
        """The effective configuration and the health checks that were run."""
        return _json(self.describe_environment())

    def resource_job(self, job_id: str) -> str:
        """One job's full record, including every step result."""
        job = self.jobs.get(job_id)
        return _json(job.to_dict())

    def resource_jobs(self) -> str:
        """The most recent jobs, newest first."""
        return _json({"jobs": [job.to_dict() for job in self.jobs.list(limit=25)]})


def _raw_common_block() -> list[str]:
    """The COMMON declaration shared with the generated modules (17 names)."""
    return [
        "  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $",
        "                              rasters, rasterNames, rasterCount, $",
        "                              failed, failure, logFile, progressFile, $",
        "                              statusFile, outDir, jobId, opName, $",
        "                              inspectJson",
    ]


class _RawIdlPlan:
    """A minimal stand-in for :class:`~envimcp.scriptgen.JobPlan`.

    ``envi_run_idl`` is the one tool whose body is not generated from a task
    definition, so it cannot reuse ``render_driver``. It only has to satisfy
    what :class:`~envimcp.runner.Runner` and :class:`~envimcp.jobs.Job` touch:
    the path properties, ``write()``, and ``to_dict()``.
    """

    def __init__(
        self,
        *,
        job_id: str,
        kind: str,
        steps: tuple,
        job_dir: Path,
        output_dir: Path,
        idl_lib_dir: Path,
        batch_path: Path,
        config: Config,
        extra_body: str,
    ) -> None:
        self.job_id = job_id
        self.kind = kind
        self.steps = steps
        self.job_dir = Path(job_dir)
        self.output_dir = Path(output_dir)
        self.idl_lib_dir = Path(idl_lib_dir)
        self.batch_path = Path(batch_path)
        self.config = config
        self.raster_op = None
        self.extra_body = extra_body

    @property
    def gen_dir(self) -> Path:
        return self.job_dir / "gen"

    @property
    def status_path(self) -> Path:
        return self.job_dir / "status.json"

    @property
    def log_path(self) -> Path:
        return self.job_dir / "idl.log"

    @property
    def progress_path(self) -> Path:
        return self.job_dir / "progress.jsonl"

    @property
    def bootstrap_path(self) -> Path:
        return self.job_dir / "bootstrap.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "job_dir": str(self.job_dir),
            "output_dir": str(self.output_dir),
            "batch_path": str(self.batch_path),
            "gen_dir": str(self.gen_dir),
            "status_path": str(self.status_path),
            "log_path": str(self.log_path),
            "progress_path": str(self.progress_path),
            "steps": [],
            "raster_op": None,
            "raw_idl": True,
        }

    def write(self) -> Path:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.gen_dir.mkdir(parents=True, exist_ok=True)
        self.batch_path.write_text(self.driver(), encoding="utf-8")
        self.bootstrap_path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return self.batch_path

    def driver(self) -> str:
        """The ``@``-included batch file: compile the library and call the body."""
        lib = self.idl_lib_dir
        main_file = self.gen_dir / "envi_dsh_rawmain.pro"
        main_file.parent.mkdir(parents=True, exist_ok=True)
        main_file.write_text(self.extra_body + "\n", encoding="utf-8")
        p = lambda path: str(path).replace("\\", "/")  # noqa: E731
        return "\n".join(
            [
                "; Generated by envimcp (envi_run_idl) -- do not edit by hand.",
                f".compile '{p(lib / 'envi_dsh_util.pro')}'",
                f".compile '{p(lib / 'envi_dsh_runner.pro')}'",
                f".compile '{p(main_file)}'",
                "",
                "ENVI_DSH_RAW_MAIN",
                "",
            ]
        )

    def main_proc_path(self) -> Path:  # pragma: no cover - parity with JobPlan
        return self.gen_dir / "envi_dsh_rawmain.pro"

    def step_proc_path(self, index: int) -> Path:  # pragma: no cover
        return self.gen_dir / f"step{index}.pro"


def build_server(config: Config | None = None):
    """Assemble the MCP server with every tool and resource registered."""
    from mcp.server.mcpserver import MCPServer

    state = EnvimcpServer(config)
    server = MCPServer(
        name=state.config.server.name,
        title="ENVI 5.2 operator bridge",
        version="0.1.0",
        instructions=(
            "Drives a local ENVI 5.2 installation through headless IDL 8.4. Typical flow: "
            "envi_list_tasks to find an operator, envi_get_task_schema for its exact "
            "parameters, then envi_run_task. Use envi_create_task_workflow when several "
            "operators feed each other. Outputs accept '#name' (outputs/ directory), "
            "'!name' (the job directory) or an absolute path. Only one ENVI job runs at a "
            "time; every result carries a job_id that envi_job_status can follow. "
            "envi_inspect_raster answers metadata questions without ENVI unless you pass "
            "use_envi=True."
        ),
    )

    @server.tool()
    def envi_list_tasks(query: str = "", limit: int = 80) -> dict[str, Any]:
        """List or search the ENVI operators available on this machine."""
        return state.envi_list_tasks(query=query, limit=limit)

    @server.tool()
    def envi_get_task_schema(name: str) -> dict[str, Any]:
        """Return the exact JSON Schema and choice values for one ENVI operator."""
        return state.envi_get_task_schema(name)

    @server.tool()
    def envi_run_task(
        task: str,
        inputs: dict[str, Any] | None = None,
        output: str | None = None,
        timeout_s: int = 0,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Run one ENVI operator and return its output raster's metadata and statistics."""
        return state.envi_run_task(
            task=task, inputs=inputs, output=output, timeout_s=timeout_s, wait=wait
        )

    @server.tool()
    def envi_create_task_workflow(
        steps: list[dict[str, Any]], timeout_s: int = 0, wait: bool = True
    ) -> dict[str, Any]:
        """Run several ENVI operators in order, wiring each output into the next."""
        return state.envi_create_task_workflow(steps=steps, timeout_s=timeout_s, wait=wait)

    @server.tool()
    def envi_inspect_raster(
        path: str, stats: bool = True, use_envi: bool = False
    ) -> dict[str, Any]:
        """Describe a raster: geometry, data type, projection, band statistics."""
        return state.envi_inspect_raster(path=path, stats=stats, use_envi=use_envi)

    @server.tool()
    def envi_export_raster(
        path: str, destination: str, format: str = "tiff", wait: bool = True
    ) -> dict[str, Any]:
        """Convert a raster to GeoTIFF or ENVI format using ENVI itself."""
        return state.envi_export_raster(
            path=path, destination=destination, format=format, wait=wait
        )

    @server.tool()
    def envi_job_status(job_id: str) -> dict[str, Any]:
        """Report one job's status, result, outputs and IDL output tail."""
        return state.envi_job_status(job_id)

    @server.tool()
    def envi_job_cancel(job_id: str) -> dict[str, Any]:
        """Cancel a queued or running job and kill its IDL process tree."""
        return state.envi_job_cancel(job_id)

    if state.config.security.allow_raw_idl:

        @server.tool(name="envi_run_idl")
        def envi_run_idl(source: str, timeout_s: int = 300) -> dict[str, Any]:
            """Escape hatch: execute arbitrary IDL source in a headless ENVI session."""
            return state.envi_run_idl(source=source, timeout_s=timeout_s)

    @server.resource(
        "envi://config",
        name="envimcp configuration and health",
        mime_type="application/json",
    )
    def envi_config_resource() -> str:
        return state.resource_config()

    @server.resource(
        "envi://tasks",
        name="The ENVITask catalog",
        mime_type="application/json",
    )
    def envi_tasks_resource() -> str:
        return state.resource_tasks()

    @server.resource(
        "envi://jobs",
        name="Recent jobs, newest first",
        mime_type="application/json",
    )
    def envi_jobs_resource() -> str:
        return state.resource_jobs()

    @server.resource(
        "envi://task/{name}",
        name="One ENVITask schema",
        mime_type="application/json",
    )
    def envi_task_resource(name: str) -> str:
        return state.resource_task(name)

    @server.resource(
        "envi://job/{job_id}",
        name="One job record",
        mime_type="application/json",
    )
    def envi_job_resource(job_id: str) -> str:
        return state.resource_job(job_id)

    server._envimcp_state = state  # type: ignore[attr-defined]
    return server


def run_server(
    *,
    transport: str = "stdio",
    config: Config | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Build and serve the MCP server over stdio or streamable HTTP."""
    server = build_server(config)
    state: EnvimcpServer = server._envimcp_state  # type: ignore[attr-defined]
    problems = state.config.validate()
    if problems:
        raise SystemExit(
            "envimcp cannot start:\n  - " + "\n  - ".join(problems)
        )
    if transport == "stdio":
        server.run(transport="stdio")
        return
    if transport in {"http", "streamable-http"}:
        server.run(
            transport="streamable-http",
            host=host or state.config.server.host,
            port=int(port or state.config.server.port),
        )
        return
    raise SystemExit(f"unknown transport {transport!r}; use 'stdio' or 'http'")


__all__ += ["EXPORT_FORMATS", "UnknownTaskError", "ValidationError", "BusyError", "JobNotFoundError"]
