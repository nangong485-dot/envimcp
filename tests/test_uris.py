"""Output-URI token semantics.

The three tokens are the whole public surface for "where does this file go":
``#name`` (persistent, ``outputs/``), ``!name`` (the job directory) and
``*name`` (virtual: a workflow intermediate that is never delivered). The
security rules around absolute paths matter because the MCP client can be an
LLM, so an unconstrained path is an unconstrained write.
"""

from __future__ import annotations

import pytest

from envimcp.uris import TEMP_PREFIX, UriError, resolve_output


def test_persistent_token_lands_in_outputs_root(config, tmp_path) -> None:
    resolved = resolve_output("#ndvi", config=config, job_dir=tmp_path)
    assert resolved.kind == "persistent"
    assert resolved.name == "ndvi.dat"  # default extension is added
    assert resolved.path == config.output_root_path / "ndvi.dat"


def test_temp_token_lands_in_the_job_directory(config, tmp_path) -> None:
    resolved = resolve_output("!step1", config=config, job_dir=tmp_path)
    assert resolved.kind == "temp"
    assert resolved.path == tmp_path / "step1.dat"


def test_virtual_token_has_no_path(config, tmp_path) -> None:
    resolved = resolve_output("*NDVI", config=config, job_dir=tmp_path)
    assert resolved.kind == "virtual"
    assert resolved.is_virtual
    assert resolved.path is None
    assert resolved.name == "NDVI"
    assert resolved.raw == "*NDVI"


def test_existing_extension_is_kept(config, tmp_path) -> None:
    assert resolve_output("!out.tif", config=config, job_dir=tmp_path).name == "out.tif"
    assert resolve_output("#out.geotiff", config=config, job_dir=tmp_path).name == "out.geotiff"


def test_all_pixels_is_rejected_as_an_output(config, tmp_path) -> None:
    with pytest.raises(UriError):
        resolve_output("**", config=config, job_dir=tmp_path)


def test_relative_path_is_rejected(config, tmp_path) -> None:
    with pytest.raises(UriError) as excinfo:
        resolve_output("relative.dat", config=config, job_dir=tmp_path)
    assert excinfo.value.kind == "RelativeOutputPath"


def test_path_outside_the_allowed_roots_is_rejected(config) -> None:
    with pytest.raises(UriError):
        resolve_output(r"C:\Windows\evil.dat", config=config, job_dir=config.jobs_path / "j")


def test_absolute_path_inside_a_root_is_accepted(config) -> None:
    target = config.jobs_path / "job-x" / "ok.dat"
    resolved = resolve_output(str(target), config=config, job_dir=config.jobs_path / "job-x")
    assert resolved.kind == "path"
    assert resolved.path == target


def test_illegal_name_characters_are_rejected(config, tmp_path) -> None:
    with pytest.raises(UriError):
        resolve_output(TEMP_PREFIX + "bad/name", config=config, job_dir=tmp_path)


def test_empty_output_is_rejected(config, tmp_path) -> None:
    with pytest.raises(UriError):
        resolve_output("   ", config=config, job_dir=tmp_path)
