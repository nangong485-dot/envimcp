"""Command-line entry points for envimcp.

Usage::

    envimcp serve --transport stdio
    envimcp serve --transport http --port 8765
    envimcp doctor
    envimcp tasks [QUERY]
    envimcp schema TASK
    envimcp run TASK --input KEY=VALUE ... [--output PATH]
    envimcp inspect PATH
    envimcp smoke

``doctor`` and ``smoke`` exist because the first question about this setup is
always "is the IDL/ENVI path actually usable?", and answering it should not
require an MCP client.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .catalog import UnknownTaskError, cached_catalog
from .config import Config, ConfigError, load_config
from .jobs import JobManager
from .raster import RasterError, cog_backend, compute_stats, raster_info
from .runner import Runner, RunnerError, which_idl
from .schema import ValidationError, task_schema
from .scriptgen import (
    PlannedStep,
    ScriptGenError,
    plan_export,
    plan_inspect,
    plan_workflow,
)

__all__ = ["main", "build_parser"]


def _config(args: argparse.Namespace) -> Config:
    try:
        return load_config(getattr(args, "config", None))
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc


def _parse_inputs(pairs: list[str]) -> dict[str, Any]:
    """Turn ``KEY=VALUE`` arguments into a typed mapping.

    Values are parsed as JSON when possible so that numbers, booleans, arrays
    and objects arrive with the right type (``ITERATIONS=6`` is an int,
    ``REVERSE_COLOR_TABLE=true`` a bool). Anything that is not valid JSON is
    passed through as a plain string, which is what paths and choice names are.
    """
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--input needs KEY=VALUE, got {pair!r}")
        try:
            out[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            out[key.strip()] = raw
    return out


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = _config(args)
    problems = config.validate()
    catalog = None
    catalog_error = ""
    try:
        catalog = cached_catalog(config.envi.task_template_dir)
    except Exception as exc:  # noqa: BLE001 - report, never traceback
        catalog_error = f"{type(exc).__name__}: {exc}"
    report = {
        "config_source": config.source,
        "config": config.describe(),
        "idl_exe": {
            "configured": config.idl.exe,
            "exists": Path(config.idl.exe).is_file(),
            "found_on_path": which_idl(config),
        },
        "catalog": {
            "task_count": len(catalog) if catalog else 0,
            "load_failures": list(catalog.load_failures) if catalog else [catalog_error],
        },
        "cog_backend": cog_backend(),
        "problems": problems,
        "verdict": "ready" if not problems and catalog else "not ready",
    }
    _print(report)
    return 0 if report["verdict"] == "ready" else 1


def _cmd_tasks(args: argparse.Namespace) -> int:
    config = _config(args)
    catalog = cached_catalog(config.envi.task_template_dir)
    specs = catalog.search(args.query) if args.query else list(catalog)
    _print(
        {
            "total_available": len(catalog),
            "matched": len(specs),
            "tasks": [
                {
                    "name": s.name,
                    "display_name": s.display_name,
                    "required_inputs": [p.name for p in s.required_inputs],
                    "outputs": [p.name for p in s.outputs],
                }
                for s in specs
            ],
        }
    )
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    config = _config(args)
    catalog = cached_catalog(config.envi.task_template_dir)
    try:
        spec = catalog.resolve(args.task, strict=True)
    except UnknownTaskError as exc:
        raise SystemExit(str(exc)) from exc
    schema = task_schema(spec)
    _print(
        {
            "task": spec.name,
            "display_name": spec.display_name,
            "description": spec.description,
            "source": spec.source_path,
            "schema": schema,
            "choices": {
                p.name: list(p.choices) for p in spec.parameters if p.has_choice_list and p.choices
            },
        }
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    config = _config(args)
    catalog = cached_catalog(config.envi.task_template_dir)
    inputs = _parse_inputs(args.input)
    # Mirrors envi_run_task: with no --output the result lands in the job
    # directory, so `run` works without knowing the ENVI parameter name.
    output = args.output or f"!{args.task.lower()}.dat"
    workflow = plan_workflow(
        catalog=catalog,
        config=config,
        steps=[
            PlannedStep(
                args.task,
                inputs=inputs,
                outputs={args.output_param: output},
            )
        ],
        kind="TASK_EXECUTE",
    )
    manager = JobManager(config)
    job = manager.submit(workflow.plan, task=args.task, background=False)
    payload = {"job_id": job.id, "status": job.status}
    if job.result is not None:
        payload.update(job.result.to_dict())
    _print(payload)
    return 0 if job.result is not None and job.result.ok else 1


def _cmd_inspect(args: argparse.Namespace) -> int:
    config = _config(args)
    payload: dict[str, Any] = {"raster": raster_info(args.path).to_dict()}
    if args.stats:
        payload["stats"] = compute_stats(args.path)
    _print(payload)
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    config = _config(args)
    plan = plan_export(
        config=config, raster=args.path, destination=args.destination, fmt=args.format
    )
    manager = JobManager(config)
    job = manager.submit(plan, kind=plan.kind, background=False)
    payload = {"job_id": job.id, "status": job.status}
    if job.result is not None:
        payload.update(job.result.to_dict())
    _print(payload)
    return 0 if job.result is not None and job.result.ok else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import run_server

    config = _config(args)
    if args.transport:
        config.server.transport = args.transport
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = int(args.port)
    try:
        run_server(
            transport=config.server.transport,
            config=config,
            host=config.server.host,
            port=config.server.port,
        )
    except RunnerError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def _cmd_smoke(args: argparse.Namespace) -> int:
    """End-to-end check: run SpectralIndex/NDVI on the bundled sample dataset.

    This is the acceptance path in miniature. It asserts the min/max/mean against
    the measured golden baseline, so a regression in the IDL side is caught here
    rather than by a user.
    """
    config = _config(args)
    problems = config.validate()
    if problems:
        _print({"verdict": "not ready", "problems": problems})
        return 1
    catalog = cached_catalog(config.envi.task_template_dir)
    workflow = plan_workflow(
        catalog=catalog,
        config=config,
        steps=[
            PlannedStep(
                "SpectralIndex",
                inputs={
                    "INPUT_RASTER": config.envi.sample_raster,
                    "INDEX": "Normalized Difference Vegetation Index",
                },
                outputs={"OUTPUT_RASTER": "!smoke_ndvi.dat"},
            )
        ],
        kind="TASK_EXECUTE",
    )
    runner = Runner(config)
    result = runner.run(workflow.plan, timeout_s=int(args.timeout))
    stats: dict[str, Any] = {}
    if result.steps:
        stats = result.steps[0].get("stats") or {}
    baseline = {"min": -0.43410853, "max": 0.62695548, "mean": 0.10097130}
    deltas = {
        key: (stats.get(key) - value) if isinstance(stats.get(key), (int, float)) else None
        for key, value in baseline.items()
    }
    matches = all(d is not None and abs(d) < args.tolerance for d in deltas.values())
    _print(
        {
            "job_id": result.job_id,
            "ok": result.ok,
            "status": result.status,
            "error": result.error,
            "elapsed_s": result.elapsed_s,
            "stats": stats,
            "baseline": baseline,
            "deltas": deltas,
            "within_tolerance": matches,
            "tolerance": args.tolerance,
            "verdict": "baseline reproduced" if result.ok and matches else "MISMATCH",
        }
    )
    return 0 if result.ok and matches else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="envimcp",
        description="Drive ENVI 5.2 operators over MCP (headless IDL bridge).",
    )
    parser.add_argument("--config", help="path to envimcp.toml (defaults to config/envimcp.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="start the MCP server")
    p_serve.add_argument("--transport", choices=["stdio", "http", "streamable-http"])
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.set_defaults(func=_cmd_serve)

    p_doctor = sub.add_parser("doctor", help="check the IDL/ENVI paths and the task catalog")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_tasks = sub.add_parser("tasks", help="list or search ENVITasks")
    p_tasks.add_argument("query", nargs="?", default="")
    p_tasks.set_defaults(func=_cmd_tasks)

    p_schema = sub.add_parser("schema", help="show one ENVITask's JSON Schema")
    p_schema.add_argument("task")
    p_schema.set_defaults(func=_cmd_schema)

    p_run = sub.add_parser("run", help="run one ENVITask")
    p_run.add_argument("task")
    p_run.add_argument(
        "--input", action="append", default=[], metavar="KEY=VALUE", help="repeatable"
    )
    p_run.add_argument("--output", help="output path or #name/!name token")
    p_run.add_argument("--output-param", default="OUTPUT_RASTER")
    p_run.set_defaults(func=_cmd_run)

    p_inspect = sub.add_parser("inspect", help="describe a raster without ENVI")
    p_inspect.add_argument("path")
    p_inspect.add_argument("--no-stats", dest="stats", action="store_false")
    p_inspect.set_defaults(func=_cmd_inspect, stats=True)

    p_export = sub.add_parser("export", help="convert a raster (GeoTIFF or ENVI)")
    p_export.add_argument("path")
    p_export.add_argument("destination")
    p_export.add_argument("--format", default="tiff", choices=["tiff", "tif", "gtiff", "envi"])
    p_export.set_defaults(func=_cmd_export)

    p_smoke = sub.add_parser("smoke", help="end-to-end NDVI check against the golden baseline")
    p_smoke.add_argument("--timeout", type=int, default=600)
    p_smoke.add_argument("--tolerance", type=float, default=1e-6)
    p_smoke.set_defaults(func=_cmd_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (
        ConfigError,
        ScriptGenError,
        ValidationError,
        UnknownTaskError,
        RasterError,
        RunnerError,
        FileNotFoundError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
