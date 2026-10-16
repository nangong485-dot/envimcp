"""Scratch smoke: plan + run one SpectralIndex NDVI job through the real IDL/ENVI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # <root>/tools/<script>.py
sys.path.insert(0, str(ROOT / "src"))

from envimcp.catalog import cached_catalog  # noqa: E402
from envimcp.config import load_config  # noqa: E402
from envimcp.runner import Runner  # noqa: E402
from envimcp.scriptgen import PlannedStep, plan_workflow  # noqa: E402


def main() -> int:
    config = load_config(ROOT / "config" / "envimcp.toml")
    print("config:", config.describe().get("source"))
    catalog = cached_catalog(config.envi.task_template_dir)
    print("tasks:", len(catalog))

    workflow = plan_workflow(
        catalog=catalog,
        config=config,
        steps=[
            PlannedStep(
                "SpectralIndex",
                inputs={
                    "INPUT_RASTER": config.envi.sample_raster,
                    "INDEX": "NDVI",
                },
                outputs={"OUTPUT_RASTER": "!out_ndvi.dat"},
            )
        ],
        kind="TASK_EXECUTE",
    )
    plan = workflow.plan
    print("job:", plan.job_id)
    plan.write()

    runner = Runner(config)
    print("cmd:", runner.command(plan))
    result = runner.run(plan, timeout_s=600)
    print("=== summary ===")
    print(result.summary())
    print("=== status.json ===")
    print(json.dumps(result.status, indent=2, ensure_ascii=False, default=str)[:6000])
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
