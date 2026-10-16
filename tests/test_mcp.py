"""The MCP surface: tool set, required arguments, resources and safety defaults.

These run the real :class:`~envimcp.server.EnvimcpServer` in-process, so they
pin the contract a client sees without needing a transport or an MCP client.
Note the field spellings: mcp 2.x uses ``input_schema`` and ``uri_template``
(1.x spelled them ``inputSchema`` / ``uriTemplate``).
"""

from __future__ import annotations

import asyncio

import pytest

from envimcp.server import EnvimcpServer, build_server

EXPECTED_TOOLS = {
    "envi_list_tasks",
    "envi_get_task_schema",
    "envi_run_task",
    "envi_create_task_workflow",
    "envi_inspect_raster",
    "envi_export_raster",
    "envi_job_status",
    "envi_job_cancel",
}


def _surface(config):
    server = build_server(config)

    async def probe():
        return (
            await server.list_tools(),
            await server.list_resources(),
            await server.list_resource_templates(),
        )

    return asyncio.run(probe())


def test_tool_set_is_exactly_the_documented_eight(config) -> None:
    tools, _, _ = _surface(config)
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_required_arguments(config) -> None:
    tools, _, _ = _surface(config)
    required = {tool.name: (tool.input_schema or {}).get("required") or [] for tool in tools}
    assert required["envi_list_tasks"] == []
    assert required["envi_get_task_schema"] == ["name"]
    assert required["envi_run_task"] == ["task"]
    assert required["envi_create_task_workflow"] == ["steps"]
    assert required["envi_inspect_raster"] == ["path"]
    assert set(required["envi_export_raster"]) == {"path", "destination"}
    assert required["envi_job_status"] == ["job_id"]
    assert required["envi_job_cancel"] == ["job_id"]


def test_resources_and_templates(config) -> None:
    _, resources, templates = _surface(config)
    assert {str(resource.uri) for resource in resources} == {
        "envi://config",
        "envi://tasks",
        "envi://jobs",
    }
    assert {template.uri_template for template in templates} == {
        "envi://task/{name}",
        "envi://job/{job_id}",
    }


def test_raw_idl_tool_is_absent_until_enabled(config) -> None:
    assert config.security.allow_raw_idl is False
    tools, _, _ = _surface(config)
    assert "envi_run_idl" not in {tool.name for tool in tools}


def test_raw_idl_refuses_when_disabled(config) -> None:
    state = EnvimcpServer(config)
    with pytest.raises(PermissionError):
        state.envi_run_idl("print, 1")


def test_config_resource_reports_health(config) -> None:
    import json

    state = EnvimcpServer(config)
    payload = json.loads(state.resource_config())
    assert payload["task_count"] == 58
    assert payload["problems"] == []
    assert payload["envi_root"].upper().endswith("ENVI52")


def test_tasks_resource_carries_the_whole_catalog(config) -> None:
    import json

    state = EnvimcpServer(config)
    payload = json.loads(state.resource_tasks())
    assert payload["count"] == 58
    assert len(payload["tasks"]) == 58


def test_list_tasks_tool(config) -> None:
    state = EnvimcpServer(config)
    listed = state.envi_list_tasks()
    assert listed["total_available"] == 58
    assert listed["returned"] <= 80
    searched = state.envi_list_tasks("spectral")
    assert "SpectralIndex" in [task["name"] for task in searched["tasks"]]


def test_get_task_schema_tool(config) -> None:
    state = EnvimcpServer(config)
    schema = state.envi_get_task_schema("SpectralIndex")
    assert schema["schema"]["required"] == ["INPUT_RASTER", "INDEX", "OUTPUT_RASTER"]
    assert schema["output_parameter"] == "OUTPUT_RASTER"
    assert len(schema["choices"]["INDEX"]) >= 60


def test_inspect_raster_answers_without_envi(config, fake_raster) -> None:
    """The default path must not spend a licence or the single job slot."""
    state = EnvimcpServer(config)
    payload = state.envi_inspect_raster(fake_raster, stats=True)
    assert payload["source"] == "direct-read"
    assert payload["raster"]["bands"] == 1
    assert payload["raster"]["samples"] == 2
    # A single-band dataset is reduced as one population, so the statistics keys
    # carry no band suffix (a multi-band one gets min_1, mean_2, ...).
    assert payload["stats"]["finite_count"] == 4


def test_export_refuses_cog_with_an_explanation(config, fake_raster, tmp_path) -> None:
    from envimcp.scriptgen import ScriptGenError, plan_export

    with pytest.raises(ScriptGenError) as excinfo:
        plan_export(config=config, raster=fake_raster, destination="!x.tif", fmt="cog")
    assert "Cloud-Optimized" in str(excinfo.value)


def test_job_status_reports_an_unknown_job_clearly(config) -> None:
    state = EnvimcpServer(config)
    with pytest.raises(Exception):
        state.envi_job_status("no-such-job")
