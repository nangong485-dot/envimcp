"""Scratch smoke: export one raster via ENVIRaster::Export."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # <root>/tools/<script>.py
sys.path.insert(0, str(ROOT / "src"))

from envimcp.config import load_config  # noqa: E402
from envimcp.runner import Runner  # noqa: E402
from envimcp.scriptgen import plan_export  # noqa: E402


def main() -> int:
    config = load_config(ROOT / "config" / "envimcp.toml")
    plan = plan_export(
        config=config,
        raster=r"D:\ENVI52\data\qb_boulder_msi",
        destination="!qb_boulder_msi_export.tif",
        fmt="tiff",
    )
    print("job:", plan.job_id)
    print("batch:", plan.batch_path)
    plan.write()
    runner = Runner(config)
    result = runner.run(plan, timeout_s=300)
    print("=== summary ===")
    print(result.summary())
    print("=== status ===")
    print(json.dumps(result.raw_status, indent=2, ensure_ascii=False, default=str)[:4000])
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
