"""Shared pytest fixtures for the envimcp suite.

The suite has two speeds:

* **fast** (default): pure Python. No IDL, no ENVI, no licence, no job slot.
  Everything that can be checked from the ``*.task`` catalog, the URI rules and
  the generated IDL text lives here.
* **slow** (``-m slow``): actually launches ``idl.exe`` against this machine's
  ENVI 5.2 installation. Deselect with ``-m "not slow"``.

The golden NDVI numbers in :data:`GOLDEN_NDVI` are not invented: they were
measured from this installation with a hand-written baseline script
(``tests/legacy/envitest.pro``) and have been reproduced byte-for-byte through
the generated job. A regression on the IDL side shows up as a delta here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from envimcp.catalog import TaskCatalog, cached_catalog
from envimcp.config import Config, load_config

#: Measured on this machine: SpectralIndex NDVI over the bundled QuickBird demo.
GOLDEN_NDVI = {
    "min": -0.43410853,
    "max": 0.62695548,
    "mean": 0.10097130,
    "stddev": 0.14894113,
    "finite_count": 1048576,
    "nrows": 1024,
    "ncolumns": 1024,
    "nbands": 1,
    "data_type": 5,
    "data_type_name": "double",
}

#: The tolerance the acceptance test uses against GOLDEN_NDVI.
NDVI_TOLERANCE = 1e-6


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def config(project_root: Path) -> Config:
    return load_config(project_root / "config" / "envimcp.toml")


@pytest.fixture(scope="session")
def catalog(config: Config) -> TaskCatalog:
    return cached_catalog(config.envi.task_template_dir)


@pytest.fixture
def fake_raster(tmp_path: Path) -> str:
    """A minimal ENVI dataset so the fast tests need no ENVI data at all.

    Returns the ENVI *basename* (``<tmp>/fake``) with a sibling ``fake.hdr``,
    which is the form ENVI addresses a dataset by. 1 band, 2x2, byte, BSQ.
    """
    base = tmp_path / "fake"
    base.with_name(base.name + ".hdr").write_text(
        "ENVI\n"
        "description = {synthetic fixture}\n"
        "samples = 2\n"
        "lines = 2\n"
        "bands = 1\n"
        "header offset = 0\n"
        "file type = ENVI Standard\n"
        "data type = 1\n"
        "interleave = bsq\n"
        "byte order = 0\n",
        encoding="ascii",
    )
    base.write_bytes(bytes([1, 2, 3, 4]))
    return str(base)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "slow: needs the local IDL/ENVI installation"
    )
