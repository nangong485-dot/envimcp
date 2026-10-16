"""Acceptance tests that really launch ``idl.exe`` against ENVI 5.2.

Deselected by default; run them with::

    pytest -m slow

The golden NDVI numbers were measured on this installation from a hand-written
baseline script (kept in ``tests/legacy/``) and are reproduced exactly through
the generated job. A drift here means the IDL side regressed.
"""

from __future__ import annotations

import pytest

from envimcp.jobs import JobManager
from envimcp.scriptgen import PlannedStep, plan_inspect, plan_workflow

pytestmark = pytest.mark.slow

GOLDEN_NDVI = {
    "min": -0.43410853,
    "max": 0.62695548,
    "mean": 0.10097130,
    "stddev": 0.14894113,
}

#: The acceptance criterion for AC1 is 1e-6 against the measured baseline.
TOLERANCE = 1e-6


def test_ndvi_reproduces_the_golden_baseline(config, catalog) -> None:
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
                outputs={"OUTPUT_RASTER": "!accept_ndvi.dat"},
            )
        ],
        kind="TASK_EXECUTE",
    )
    job = JobManager(config).submit(workflow.plan, task="SpectralIndex", background=False)
    assert job.result is not None, "the runner produced no result at all"
    assert job.result.ok, job.result.error

    step = job.result.steps[0]
    assert step["status"] is True
    for key, expected in GOLDEN_NDVI.items():
        actual = step["stats"][key]
        assert abs(actual - expected) < TOLERANCE, f"{key}: {actual} != {expected}"
    raster = step["raster"]
    assert (raster["nrows"], raster["ncolumns"], raster["nbands"]) == (1024, 1024, 1)
    assert raster["data_type"] == 5 and raster["data_type_name"] == "double"
    assert step["stats"]["finite_count"] == 1048576


def test_workflow_carries_a_virtual_raster_between_steps(config, catalog) -> None:
    """AC6: step 1 writes no file; step 2 consumes its runtime handle."""
    workflow = plan_workflow(
        catalog=catalog,
        config=config,
        steps=[
            PlannedStep(
                "SpectralIndex",
                inputs={"INPUT_RASTER": config.envi.sample_raster, "INDEX": "NDVI"},
                outputs={"OUTPUT_RASTER": "*NDVI"},
            ),
            PlannedStep(
                "LinearPercentStretchRaster",
                inputs={"INPUT_RASTER": "*NDVI", "PERCENT": 2.0},
                outputs={"OUTPUT_RASTER": "!accept_stretch.dat"},
            ),
        ],
        kind="TASK_WORKFLOW",
    )
    job = JobManager(config).submit(workflow.plan, kind="TASK_WORKFLOW", background=False)
    assert job.result is not None, "the runner produced no result at all"
    assert job.result.ok, job.result.error
    assert len(job.result.steps) == 2
    assert all(step.get("status") for step in job.result.steps)
    # The first step's NDVI statistics must still be the golden ones.
    first = job.result.steps[0]["stats"]
    assert abs(first["min"] - GOLDEN_NDVI["min"]) < TOLERANCE
    assert abs(first["mean"] - GOLDEN_NDVI["mean"]) < TOLERANCE


def test_inspect_job_reports_every_band(config) -> None:
    job = JobManager(config).submit(
        plan_inspect(config=config, raster=config.envi.sample_raster, stats=True),
        kind="INSPECT_RASTER",
        background=False,
    )
    assert job.result is not None, "the runner produced no result at all"
    assert job.result.ok, job.result.error
    inspect = job.result.raw_status["inspect"]
    assert inspect["raster"]["nbands"] == 4
    assert inspect["raster"]["data_type_name"] == "uint"
    for band in range(1, 5):
        assert f"min_{band}" in inspect["stats"]
        assert f"mean_{band}" in inspect["stats"]


def test_export_writes_a_real_geotiff(config) -> None:
    from envimcp.scriptgen import plan_export

    job = JobManager(config).submit(
        plan_export(
            config=config,
            raster=config.envi.sample_raster,
            destination="!accept_export.tif",
            fmt="tiff",
        ),
        kind="EXPORT_RASTER",
        background=False,
    )
    assert job.result is not None, "the runner produced no result at all"
    assert job.result.ok, job.result.error
    written = job.result.raw_status["inspect"]["export_path"]
    payload = open(written, "rb").read(4)
    assert payload in (b"II*\x00", b"MM\x00*"), f"not a TIFF: {payload!r}"
