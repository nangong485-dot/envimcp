"""Scratch smoke: inspect one raster through the real IDL/ENVI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # <root>/tools/<script>.py
sys.path.insert(0, str(ROOT / "src"))

from envimcp.config import load_config  # noqa: E402
from envimcp.runner import Runner  # noqa: E402
from envimcp.scriptgen import plan_inspect  # noqa: E402


def main() -> int:
    config = load_config(ROOT / "config" / "envimcp.toml")
    plan = plan_inspect(config=config, raster=config.envi.sample_raster, stats=True)
    print("job:", plan.job_id)
    plan.write()
    runner = Runner(config)
    result = runner.run(plan, timeout_s=300)
    print("=== summary ===")
    print(result.summary())
    print("=== status.json ===")
    print(json.dumps(result.status, indent=2, ensure_ascii=False, default=str)[:6000])
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
