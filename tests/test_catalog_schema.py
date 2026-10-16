"""Catalog and parameter-schema tests: the *.task files are the source of truth.

These are pure Python and need no IDL. They pin down the three quirks measured on
this installation: choiceList appears in two shapes, the legacy `keyword` often
differs from the documented parameter `name`, and an acronym like NDVI is NOT a
member of SpectralIndex.INDEX's choice list.
"""

from __future__ import annotations

import pytest

from envimcp.catalog import UnknownTaskError
from envimcp.schema import ValidationError, normalize_inputs, task_schema


def test_catalog_has_58_tasks(catalog) -> None:
    assert len(catalog) == 58


def test_names_are_sorted_and_unique(catalog) -> None:
    names = catalog.names
    assert names == sorted(names)
    assert len(set(names)) == len(names) == 58


def test_alias_resolution_is_case_insensitive(catalog) -> None:
    assert catalog.resolve("spectralindex").name == "SpectralIndex"
    assert catalog.resolve("SpectralIndex").name == "SpectralIndex"


def test_unknown_task_reports_near_misses(catalog) -> None:
    with pytest.raises(UnknownTaskError) as exc:
        catalog.resolve("SpectralIndx", strict=True)
    assert "SpectralIndex" in exc.value.candidates


def test_search_finds_by_name(catalog) -> None:
    assert "SpectralIndex" in [s.name for s in catalog.search("spectral")]


def test_choice_list_array_form(catalog) -> None:
    index = catalog.resolve("SpectralIndex").get("INDEX")
    assert index is not None and index.has_choice_list
    assert len(index.choices) >= 60
    assert "Normalized Difference Vegetation Index" in index.choices
    # The acronym is NOT a member -- this is the trap the tool descriptions warn about.
    assert "NDVI" not in index.choices


def test_choice_list_object_form(catalog) -> None:
    calibration = catalog.resolve("RadiometricCalibration").get("CALIBRATION_TYPE")
    assert calibration is not None and calibration.has_choice_list
    assert set(calibration.choices) == {
        "Radiance",
        "Top-of-Atmosphere Reflectance",
        "Brightness Temperature",
    }
    assert calibration.choice_values == (0, 1, 2)
    assert calibration.default == "Radiance"


def test_keyword_diverges_from_parameter_name(catalog) -> None:
    assert catalog.resolve("SpectralIndex").get("OUTPUT_RASTER").keyword == "OUT_FILENAME"
    assert catalog.resolve("RadiometricCalibration").get("INPUT_RASTER").keyword == "TARGET"


def test_output_for_prefers_the_raster(catalog) -> None:
    assert catalog.resolve("SpectralIndex").output_for().name == "OUTPUT_RASTER"


def test_task_schema_is_callable_shape(catalog) -> None:
    schema = task_schema(catalog.resolve("SpectralIndex"))
    assert schema["required"] == ["INPUT_RASTER", "INDEX", "OUTPUT_RASTER"]
    assert schema["properties"]["INDEX"]["enum"]
    assert schema["x-envi-output-parameter"] == "OUTPUT_RASTER"
    assert schema["additionalProperties"] is False


def test_ndvi_acronym_resolves_to_the_long_name(catalog) -> None:
    spec = catalog.resolve("SpectralIndex")
    out = normalize_inputs(spec, {"INPUT_RASTER": r"D:\ENVI52\data\qb_boulder_msi", "INDEX": "NDVI"})
    assert out["INDEX"] == "Normalized Difference Vegetation Index"


def test_missing_required_input_is_reported(catalog) -> None:
    with pytest.raises(ValidationError) as exc:
        normalize_inputs(catalog.resolve("SpectralIndex"), {"INDEX": "NDVI"})
    assert any("INPUT_RASTER" in message for message in exc.value.errors)


def test_unknown_parameter_is_reported(catalog) -> None:
    with pytest.raises(ValidationError) as exc:
        normalize_inputs(catalog.resolve("SpectralIndex"), {"NOPE": 1})
    assert any("NOPE" in message for message in exc.value.errors)


def test_open_ended_array_accepts_a_scalar(catalog) -> None:
    """ENVI documents DOUBLE[*] as "a value, or array of values", so a scalar promotes."""
    spec = catalog.resolve("LinearPercentStretchRaster")
    out = normalize_inputs(spec, {"INPUT_RASTER": "somewhere", "PERCENT": 2.0})
    assert out["PERCENT"] == [2.0]


def test_legacy_keyword_is_accepted_as_an_alias(catalog) -> None:
    """TARGET is RadiometricCalibration's keyword for INPUT_RASTER."""
    spec = catalog.resolve("RadiometricCalibration")
    out = normalize_inputs(spec, {"TARGET": "somewhere"})
    assert out["INPUT_RASTER"] == "somewhere"
