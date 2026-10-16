"""Scratch smoke: exercise the MCP server surface end to end.

Runs the real server object in-process: initialize, tools/list, resources/list,
then a real NDVI task call, and finally the same through the task-workflow path.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # <root>/tools/<script>.py
sys.path.insert(0, str(ROOT / "src"))

from envimcp.config import load_config  # noqa: E402
from envimcp.server import EnvimcpServer, build_server  # noqa: E402


def main() -> int:
    config = load_config(ROOT / "config" / "envimcp.toml")
    server = build_server(config)
    state: EnvimcpServer = server._envimcp_state

    async def probe() -> None:
        tools = await server.list_tools()
        print("=== tools/%d ===" % len(tools))
        for tool in tools:
            # mcp 2.x names this `input_schema`; 1.x called it `inputSchema`.
            required = (tool.input_schema or {}).get("required") or []
            print(f"  {tool.name}: required={required}")

        resources = await server.list_resources()
        print("=== static resources/%d ===" % len(resources))
        for res in resources:
            print(f"  {res.uri}")

        templates = await server.list_resource_templates()
        print("=== resource templates/%d ===" % len(templates))
        for tpl in templates:
            # mcp 2.x: `uri_template` (1.x: `uriTemplate`).
            print(f"  {tpl.uri_template}  (mime={tpl.mime_type})")

        print("=== envi://config (head) ===")
        print(state.resource_config()[:300])

    asyncio.run(probe())

    print("=== envi_get_task_schema(SpectralIndex) ===")
    schema = state.envi_get_task_schema("SpectralIndex")
    print("task:", schema["task"], "| output_parameter:", schema["output_parameter"])
    print("required:", schema["schema"].get("required"))
    print("INDEX choices:", len(schema["choices"].get("INDEX", [])))
    print("first 3 choices:", schema["choices"].get("INDEX", [])[:3])

    print("=== envi_inspect_raster (direct read) ===")
    info = state.envi_inspect_raster(config.envi.sample_raster, stats=False)
    print("source:", info["source"], "| bands:", info["raster"]["bands"],
          "| type:", info["raster"]["data_type_name"],
          "| size_ok:", info["raster"]["size_matches_header"])

    print("=== envi_run_task(SpectralIndex -> NDVI) ===")
    result = state.envi_run_task(
        task="SpectralIndex",
        inputs={"INPUT_RASTER": config.envi.sample_raster, "INDEX": "NDVI"},
        output="!mcp_ndvi.dat",
        timeout_s=600,
        wait=True,
    )
    print("ok:", result.get("ok"), "| status:", result.get("status"),
          "| exit:", result.get("exit_code"), "| elapsed:", result.get("elapsed_s"))
    if result.get("error"):
        print("ERROR:", result["error"])
    steps = result.get("steps") or []
    if steps:
        stats = steps[0].get("stats") or {}
        print("stats:", {k: stats.get(k) for k in ("min", "max", "mean", "stddev")})
        print("raster:", {k: (steps[0].get("raster") or {}).get(k) for k in ("nrows", "ncolumns", "nbands", "data_type_name")})

    print("=== envi_job_status ===")
    status = state.envi_job_status(result["job_id"])
    print("status:", status["status"], "| running:", status["running"])

    print("=== envi_create_task_workflow (2 steps, virtual wiring) ===")
    # RadiometricCalibration needs sensor gain/offset metadata that the bundled
    # QuickBird demo lacks, so this chain uses a plain stretch as the consumer.
    # Step 1 writes NO file ('*NDVI'); step 2 consumes that handle directly, which
    # is what validates the virtual-raster registry.
    workflow = state.envi_create_task_workflow(
        steps=[
            {
                "task": "SpectralIndex",
                "inputs": {"INPUT_RASTER": config.envi.sample_raster, "INDEX": "NDVI"},
                "outputs": {"OUTPUT_RASTER": "*NDVI"},
            },
            {
                "task": "LinearPercentStretchRaster",
                "inputs": {"INPUT_RASTER": "*NDVI", "PERCENT": 2.0},
                "outputs": {"OUTPUT_RASTER": "!wf_stretch.dat"},
            },
        ],
        timeout_s=900,
        wait=True,
    )
    print("ok:", workflow.get("ok"), "| status:", workflow.get("status"),
          "| kind:", workflow.get("kind"))
    if workflow.get("error"):
        print("ERROR:", workflow["error"])
    for step in workflow.get("steps") or []:
        print("  step", step.get("index"), step.get("task"), step.get("status"),
              "| stats:", {k: (step.get("stats") or {}).get(k) for k in ("min", "max", "mean")})
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
