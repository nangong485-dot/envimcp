"""Invariants of the generated IDL, and pre-flight rejection (AC3).

Every assertion in this file corresponds to a failure that was actually observed
against IDL 8.4 and ENVI 5.2, so each one is a regression guard for a trap that
already cost a debugging session. Keeping them here means the next person to
touch ``scriptgen.py`` finds out immediately rather than after a silent no-op.
"""

from __future__ import annotations

import pytest

from envimcp.scriptgen import (
    PlannedStep,
    ScriptGenError,
    main_proc_name,
    plan_workflow,
)
from envimcp.uris import VIRTUAL_PREFIX


def _plan(config, catalog, raster, outputs, *, kind="TASK_EXECUTE", jid="t-job", job_dir=None):
    workflow = plan_workflow(
        catalog=catalog,
        config=config,
        steps=[
            PlannedStep(
                "SpectralIndex",
                inputs={"INPUT_RASTER": raster, "INDEX": "NDVI"},
                outputs=outputs,
            )
        ],
        kind=kind,
        job_id_value=jid,
        job_dir=job_dir,
    )
    return workflow.plan


@pytest.fixture
def ndvi_plan(config, catalog, fake_raster, tmp_path):
    plan = _plan(config, catalog, fake_raster, {"OUTPUT_RASTER": "!out.dat"}, job_dir=tmp_path / "job")
    plan.write()
    return plan


def test_entry_batch_only_compiles_then_calls(ndvi_plan) -> None:
    text = ndvi_plan.batch_path.read_text(encoding="utf-8")
    assert ".compile " in text
    # `.compile, 'x'` is not an executive command; the comma form fails outright.
    assert ".compile," not in text
    # A trailing bare END is a syntax error in an -e batch file.
    assert not text.rstrip().endswith("END")
    assert main_proc_name(ndvi_plan) in text


def test_generated_modules_do_not_call_hash(ndvi_plan) -> None:
    """HASH() is not resolvable from `.compile`d modules in -e batch mode.

    ENVI_DSH_JHASH is this project's own serializer and is fine; what must never
    appear is a call to IDL's built-in HASH() constructor.
    """
    for path in (ndvi_plan.step_proc_path(0), ndvi_plan.main_proc_path):
        # Comments mention HASH() on purpose (they explain why it is avoided), so
        # only executable lines are inspected.
        code = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith(";")
        ).replace("ENVI_DSH_JHASH", "")
        assert "HASH(" not in code


def test_routine_names_fit_the_idl_32_char_limit(ndvi_plan) -> None:
    name = main_proc_name(ndvi_plan)
    assert len(name) <= 32
    assert len(ndvi_plan.step_proc_path(0).stem) <= 32
    # IDL truncates identifiers at 32 characters. The file name must equal the
    # routine name, otherwise `.compile` registers a different name than the call
    # site resolves and the run dies with a misleading "undefined function".
    assert ndvi_plan.main_proc_path.stem == name.lower()


def test_close_happens_before_exit(ndvi_plan) -> None:
    text = ndvi_plan.main_proc_path.read_text(encoding="utf-8")
    assert "e.Close" in text and "EXIT" in text
    assert text.index("e.Close") < text.index("EXIT")


def test_primary_output_uses_the_uri_companion_property(ndvi_plan) -> None:
    """`t.OUTPUT_RASTER = '<path>'` is rejected: the parameter wants an ENVIRASTER."""
    text = ndvi_plan.step_proc_path(0).read_text(encoding="utf-8")
    assert "OUTPUT_RASTER_URI" in text
    assert "t.OUTPUT_RASTER =" not in text


def test_host_token_is_never_handed_to_envi_literally(ndvi_plan) -> None:
    """ENVI writes literally where the URI says, so '!name' must be resolved."""
    text = ndvi_plan.step_proc_path(0).read_text(encoding="utf-8")
    assert "'!out.dat'" not in text


def test_virtual_output_writes_to_a_temp_file(config, catalog, fake_raster, tmp_path) -> None:
    """ENVI 5.2 has no virtual-output mode, so only the HANDLE can travel.

    A virtual output can only be a non-final step -- a workflow whose last step
    writes nothing is refused -- so this needs a two-step chain.
    """
    workflow = plan_workflow(
        catalog=catalog,
        config=config,
        steps=[
            PlannedStep(
                "SpectralIndex",
                inputs={"INPUT_RASTER": fake_raster, "INDEX": "NDVI"},
                outputs={"OUTPUT_RASTER": "*NDVI"},
            ),
            PlannedStep(
                "LinearPercentStretchRaster",
                inputs={"INPUT_RASTER": "*NDVI", "PERCENT": 2.0},
                outputs={"OUTPUT_RASTER": "!out.dat"},
            ),
        ],
        kind="TASK_WORKFLOW",
        job_id_value="t-virt",
        job_dir=tmp_path / "v",
    )
    plan = workflow.plan
    plan.write()
    text = plan.step_proc_path(0).read_text(encoding="utf-8")
    assert VIRTUAL_PREFIX + "NDVI" in text
    assert "GetTemporaryFilename" in text
    # The consumer step reads back the registered handle, not a path on disk.
    consumer = plan.step_proc_path(1).read_text(encoding="utf-8")
    assert "ENVI_DSH_VIRTUAL" in consumer


def test_a_virtual_final_output_is_refused(config, catalog, fake_raster, tmp_path) -> None:
    with pytest.raises(ScriptGenError):
        _plan(
            config,
            catalog,
            fake_raster,
            {"OUTPUT_RASTER": "*NDVI"},
            kind="TASK_WORKFLOW",
            jid="t-x",
            job_dir=tmp_path / "x",
        )


def test_unknown_parameter_is_rejected_before_anything_runs(config, catalog, fake_raster, tmp_path) -> None:
    """AC3: the rejection is Python-side, so no job directory ever appears."""
    target = tmp_path / "never"
    with pytest.raises(ScriptGenError):
        plan_workflow(
            catalog=catalog,
            config=config,
            steps=[
                PlannedStep(
                    "SpectralIndex",
                    inputs={"INPUT_RASTER": fake_raster, "BOGUS": 1},
                    outputs={"OUTPUT_RASTER": "!o.dat"},
                )
            ],
            job_id_value="t-bad",
            job_dir=target,
        )
    assert not target.exists()


def test_missing_required_parameter_is_rejected_before_anything_runs(
    config, catalog, fake_raster, tmp_path
) -> None:
    target = tmp_path / "missing"
    with pytest.raises(ScriptGenError):
        plan_workflow(
            catalog=catalog,
            config=config,
            steps=[
                PlannedStep(
                    "SpectralIndex",
                    inputs={"INPUT_RASTER": fake_raster},
                    outputs={"OUTPUT_RASTER": "!o.dat"},
                )
            ],
            job_id_value="t-missing",
            job_dir=target,
        )
    assert not target.exists()


def test_unknown_task_is_rejected(config, catalog, fake_raster, tmp_path) -> None:
    with pytest.raises(Exception):  # UnknownTaskError, wrapped by plan_workflow
        plan_workflow(
            catalog=catalog,
            config=config,
            steps=[
                PlannedStep(
                    "NotARealTask",
                    inputs={"INPUT_RASTER": fake_raster},
                    outputs={"OUTPUT_RASTER": "!o.dat"},
                )
            ],
            job_id_value="t-unknown",
            job_dir=tmp_path / "u",
        )
    assert not (tmp_path / "u").exists()
