"""Contract tests for the two behaviours tightened during the step-9 review.

Both exist because the review found a gap between what the code *said* and what
it *did*:

* **S1** — a step declaring two output destinations silently produced only the
  first while still reporting ``ok=true``. ``plan_workflow`` now refuses it.
* **B1/B2** — ``limits.max_retries`` was documented but never read. It is now
  honoured, and only for TRANSIENT failures: a deterministic task error would
  fail identically on every attempt and merely burn the single ENVI licence.

The retry test uses a stub runner, so it needs no IDL and no licence.
"""

from __future__ import annotations

import pytest

from envimcp.jobs import TRANSIENT_ERROR_KINDS, JobManager
from envimcp.runner import JobResult
from envimcp.scriptgen import PlannedStep, ScriptGenError, plan_workflow


class _StubRunner:
    """Records how many times it was asked to run, and replays scripted results."""

    def __init__(self, results: list[JobResult]) -> None:
        self.results = list(results)
        self.calls = 0

    def run(self, plan, *, timeout_s=None, cancel_event=None) -> JobResult:
        self.calls += 1
        if not self.results:
            raise AssertionError("the runner was called more often than scripted")
        return self.results.pop(0)

    def terminate_orphans(self, *, plan=None) -> list[int]:
        return []


def _plan(config, catalog, raster, outputs, *, jid, job_dir, task="SpectralIndex"):
    return plan_workflow(
        catalog=catalog,
        config=config,
        steps=[
            PlannedStep(
                task,
                inputs={"INPUT_RASTER": raster, "INDEX": "NDVI"},
                outputs=outputs,
            )
        ],
        kind="TASK_EXECUTE",
        job_id_value=jid,
        job_dir=job_dir,
    ).plan


def _multi_output_task(catalog):
    """A task with two raster/URI outputs and exactly one required raster input."""
    for spec in catalog:
        outs = [p for p in spec.outputs if p.is_raster or p.is_uri]
        required = [p for p in spec.required_inputs]
        if len(outs) >= 2 and len(required) == 1 and required[0].is_raster:
            return spec, required[0], outs
    return None


def test_second_output_destination_is_refused(config, catalog, fake_raster, tmp_path) -> None:
    """S1: one output per step, refused loudly instead of silently dropped."""
    found = _multi_output_task(catalog)
    if found is None:
        pytest.skip("no catalog task exposes two raster outputs on one raster input")
    spec, input_param, outs = found

    target = tmp_path / "multi"
    with pytest.raises(ScriptGenError) as excinfo:
        plan_workflow(
            catalog=catalog,
            config=config,
            steps=[
                PlannedStep(
                    spec.name,
                    inputs={input_param.name: fake_raster},
                    outputs={outs[0].name: "!one.dat", outs[1].name: "!two.dat"},
                )
            ],
            job_id_value="t-multi",
            job_dir=target,
        )
    message = str(excinfo.value)
    assert "ONE output destination per step" in message
    # Still a pre-flight refusal: nothing was created, no IDL ran.
    assert not target.exists()


def test_transient_failure_is_retried(config, catalog, fake_raster, tmp_path) -> None:
    """B1/B2: limits.max_retries is honoured for transport-level failures."""
    manager = JobManager(config, reap_orphans=False)
    manager.config.limits.max_retries = 2
    plan = _plan(
        config,
        catalog,
        fake_raster,
        {"OUTPUT_RASTER": "!r.dat"},
        jid="t-retry",
        job_dir=tmp_path / "retry",
    )
    stub = _StubRunner(
        [
            JobResult(job_id="t-retry", status="failed", error="no status", error_kind="no_status"),
            JobResult(job_id="t-retry", status="succeeded", ok=True),
        ]
    )
    manager.runner = stub  # type: ignore[assignment]

    job = manager.submit(plan, background=False)

    assert stub.calls == 2, "a transient failure must be retried exactly once here"
    assert job.status == "succeeded"
    assert job.attempts == 2
    assert "retried after transient failure" in job.error


def test_deterministic_failure_is_not_retried(config, catalog, fake_raster, tmp_path) -> None:
    """A bad parameter would fail identically; retrying only burns the licence."""
    manager = JobManager(config, reap_orphans=False)
    manager.config.limits.max_retries = 3
    plan = _plan(
        config,
        catalog,
        fake_raster,
        {"OUTPUT_RASTER": "!d.dat"},
        jid="t-det",
        job_dir=tmp_path / "det",
    )
    stub = _StubRunner(
        [JobResult(job_id="t-det", status="failed", error="bad INDEX", error_kind="task_error")]
    )
    manager.runner = stub  # type: ignore[assignment]

    job = manager.submit(plan, background=False)

    assert stub.calls == 1, "a task_error must not be retried"
    assert job.status == "failed"
    assert job.attempts == 1


def test_transient_kinds_are_the_documented_set() -> None:
    assert TRANSIENT_ERROR_KINDS == {"timeout", "no_status", "busy", "license"}
