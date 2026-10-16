"""ENVI dataset introspection and optional format conversion.

This is the layer that does *not* need ENVI: an ENVI dataset is a flat binary
file next to an ASCII ``.hdr`` sidecar, so its geometry, data type, projection
and statistics are all readable from Python. That matters because it gives the
MCP tools something cheap to answer with -- ``envi_inspect_raster`` returns
metadata without spending an ENVI licence or the single job slot.

Two deliberate limits:

* **Reads use the standard library only** (``array``, ``struct``). numpy is not
  installed in this project's virtual environment, so pulling it in for a
  mean/min/max would be a heavy dependency for a small job. ``MAX_SCAN_PIXELS``
  caps how much is read so a multi-gigabyte raster cannot exhaust memory.
* **Cloud-Optimized GeoTIFF is optional and usually unavailable here.** ENVI 5.2
  has no COG writer (``ENVIRaster::Export`` offers only 'envi', 'tiff', 'nitf'),
  so ``export_cog`` reports clearly whether GDAL/Rasterio can do it instead of
  pretending to succeed.
"""

from __future__ import annotations

import array
import importlib.util
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "RasterError",
    "EnviHeader",
    "RasterInfo",
    "ENVI_DATA_TYPES",
    "DATA_TYPE_CODES",
    "type_name",
    "find_header",
    "parse_header",
    "raster_info",
    "compute_stats",
    "export_cog",
    "cog_backend",
    "MAX_SCAN_PIXELS",
]

#: ENVI data type code -> (name, struct code, bytes per element).
#: These are ENVI's codes, which are *not* identical to IDL's SIZE() codes
#: (ENVI 3 is long while IDL 3 is long too, but ENVI 6 is complex and ENVI 9 is
#: complex double). ``ENVI_DSH_TYPE_CODE`` in ``idl/envi_dsh_util.pro`` mirrors
#: this table for the IDL side.
ENVI_DATA_TYPES: dict[int, tuple[str, str, int]] = {
    1: ("byte", "B", 1),
    2: ("int", "h", 2),
    3: ("long", "i", 4),
    4: ("float", "f", 4),
    5: ("double", "d", 8),
    6: ("complex", None, 8),
    9: ("complex double", None, 16),
    12: ("uint", "H", 2),
    13: ("ulong", "I", 4),
    14: ("long64", "q", 8),
    15: ("ulong64", "Q", 8),
}

DATA_TYPE_CODES: dict[str, int] = {name: code for code, (name, _, _) in ENVI_DATA_TYPES.items()}

#: Words that may appear as ``data type = ...`` in a hand-written header.
_DATA_TYPE_ALIASES: dict[str, int] = {
    **DATA_TYPE_CODES,
    "unsigned int": 12,
    "unsigned integer": 12,
    "short": 2,
    "2": 2,
    "unsigned short": 12,
    "1": 1,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "9": 9,
    "12": 12,
    "13": 13,
    "14": 14,
    "15": 15,
}

#: Upper bound on how many pixels a statistics pass will read.
MAX_SCAN_PIXELS = 40_000_000


class RasterError(RuntimeError):
    """Raised when a dataset cannot be read as an ENVI raster."""


def type_name(code: int | str | None) -> str:
    """Human-readable name for an ENVI data type code (or a name already)."""
    if code is None:
        return ""
    if isinstance(code, str):
        return code.strip()
    entry = ENVI_DATA_TYPES.get(int(code))
    return entry[0] if entry else f"type {int(code)}"


@dataclass(frozen=True)
class EnviHeader:
    """The parsed contents of an ENVI ``.hdr`` file."""

    path: Path
    values: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key.lower(), default)

    @property
    def samples(self) -> int:
        return int(self.get("samples", 0) or 0)

    @property
    def lines(self) -> int:
        return int(self.get("lines", 0) or 0)

    @property
    def bands(self) -> int:
        return int(self.get("bands", 1) or 1)

    @property
    def data_type(self) -> int:
        raw = self.get("data type", 0)
        if isinstance(raw, int):
            return raw
        try:
            return _DATA_TYPE_ALIASES[str(raw).strip().lower()]
        except KeyError as exc:
            raise RasterError(f"unrecognised data type {raw!r} in {self.path}") from exc

    @property
    def interleave(self) -> str:
        return str(self.get("interleave", "bsq") or "bsq").lower()

    @property
    def byte_order(self) -> int:
        return int(self.get("byte order", 0) or 0)

    @property
    def header_offset(self) -> int:
        return int(self.get("header offset", 0) or 0)

    def to_dict(self) -> dict[str, Any]:
        keep = (
            "samples",
            "lines",
            "bands",
            "data type",
            "interleave",
            "byte order",
            "header offset",
            "map info",
            "coordinate system string",
            "projection info",
            "band names",
            "wavelength",
            "wavelength units",
            "data gain values",
            "data offset values",
            "file type",
            "description",
        )
        return {k: self.get(k) for k in keep if self.get(k) is not None}


def find_header(dataset: str | Path) -> Path:
    """Locate the ``.hdr`` for *dataset*.

    Accepts the binary file (``foo.dat``), the header itself (``foo.hdr``), or
    the ENVI basename (``foo``), which is the form ENVI itself uses.
    """
    p = Path(dataset).expanduser()
    if p.suffix.lower() == ".hdr":
        if not p.is_file():
            raise RasterError(f"header not found: {p}")
        return p
    for candidate in (Path(str(p) + ".hdr"), p.with_suffix(".hdr")):
        if candidate.is_file():
            return candidate
    if p.is_file():
        raise RasterError(
            f"no ENVI header for {p}: expected {p.name}.hdr or {p.with_suffix('.hdr').name}"
        )
    raise RasterError(f"dataset not found: {p}")


def parse_header(dataset: str | Path) -> EnviHeader:
    """Parse an ENVI header file into key/value pairs.

    Values are reduced to ``int``/``float`` when they look numeric, braces are
    stripped, and long ``key = {`` blocks spanning several lines are joined --
    that is the shape ENVI writes for ``band names``, ``map info`` and friends.
    """
    hdr = find_header(dataset)
    try:
        text = hdr.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise RasterError(f"cannot read {hdr}: {exc}") from exc

    values: dict[str, Any] = {}
    pending_key: str | None = None
    pending: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if pending_key is not None:
            pending.append(line)
            if "}" in line:
                values[pending_key] = _coerce(" ".join(pending))
                pending_key = None
                pending = []
            continue
        if not line or line.startswith(";") or line.lower().startswith("envi"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if value.startswith("{") and "}" not in value:
            pending_key = key
            pending = [value]
            continue
        values[key] = _coerce(value)

    if pending_key is not None:  # unterminated brace block: keep what we have
        values[pending_key] = _coerce(" ".join(pending))
    return EnviHeader(path=hdr, values=values)


def _coerce(value: str) -> Any:
    text = value.strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    text = text.strip().strip(",").strip()
    if text == "":
        return ""
    if "," in text:
        return [_coerce(part) for part in text.split(",") if part.strip() != ""]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


@dataclass(frozen=True)
class RasterInfo:
    """Everything knowable about a dataset without opening ENVI."""

    dataset: str
    header: str
    samples: int
    lines: int
    bands: int
    data_type: int
    data_type_name: str
    interleave: str
    byte_order: int
    header_offset: int
    file_size: int
    expected_size: int
    band_names: list[str] | None = None
    map_info: Any = None
    coordinate_system: Any = None
    wavelength: Any = None
    wavelength_units: Any = None
    description: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "header": self.header,
            "samples": self.samples,
            "lines": self.lines,
            "bands": self.bands,
            "data_type": self.data_type,
            "data_type_name": self.data_type_name,
            "interleave": self.interleave,
            "byte_order": self.byte_order,
            "band_names": self.band_names,
            "map_info": self.map_info,
            "coordinate_system": self.coordinate_system,
            "wavelength": self.wavelength,
            "wavelength_units": self.wavelength_units,
            "description": self.description,
            "file_size": self.file_size,
            "expected_size": self.expected_size,
            "size_matches_header": self.file_size == self.expected_size,
        }


def raster_info(dataset: str | Path) -> RasterInfo:
    """Read the header and describe the dataset."""
    hdr = parse_header(dataset)
    data_path = _data_path(hdr)
    element = ENVI_DATA_TYPES.get(hdr.data_type, ("", None, 0))[2]
    if element == 0:
        raise RasterError(
            f"data type {hdr.data_type} ({type_name(hdr.data_type)}) is complex or unknown, which "
            "this reader does not support"
        )
    expected = hdr.samples * hdr.lines * hdr.bands * element + hdr.header_offset
    size = data_path.stat().st_size if data_path.is_file() else 0
    band_names = hdr.get("band names")
    return RasterInfo(
        dataset=str(data_path),
        header=str(hdr.path),
        samples=hdr.samples,
        lines=hdr.lines,
        bands=hdr.bands,
        data_type=hdr.data_type,
        data_type_name=type_name(hdr.data_type),
        interleave=hdr.interleave,
        byte_order=hdr.byte_order,
        header_offset=hdr.header_offset,
        file_size=size,
        expected_size=expected,
        band_names=band_names if isinstance(band_names, list) else None,
        map_info=hdr.get("map info"),
        coordinate_system=hdr.get("coordinate system string") or hdr.get("projection info"),
        wavelength=hdr.get("wavelength"),
        wavelength_units=hdr.get("wavelength units"),
        description=hdr.get("description"),
    )


def _data_path(hdr: EnviHeader) -> Path:
    """The binary file that belongs to a header."""
    if hdr.path.name.lower().endswith(".hdr"):
        stem = hdr.path.name[: -len(".hdr")]
        candidate = hdr.path.with_name(stem)
        if candidate.is_file():
            return candidate
        # ENVI also writes foo.dat + foo.hdr; prefer the .dat spelling then.
        alt = hdr.path.with_suffix(".dat")
        if alt.is_file():
            return alt
        return candidate
    return hdr.path


def compute_stats(
    dataset: str | Path,
    *,
    max_pixels: int = MAX_SCAN_PIXELS,
    per_band: bool | None = None,
) -> dict[str, Any]:
    """min/max/mean/stddev over the dataset, per band when it has several.

    Uses the standard library only. Returns a ``stats_error`` key instead of
    raising when the data cannot be reduced, so a caller can still report the
    geometry it did manage to read.
    """
    info = raster_info(dataset)
    hdr = parse_header(dataset)
    data_path = Path(info.dataset)
    element = ENVI_DATA_TYPES[info.data_type][2]
    struct_code = ENVI_DATA_TYPES[info.data_type][1]
    if struct_code is None:  # pragma: no cover - guarded in raster_info
        return {"stats_error": f"data type {info.data_type_name} is not supported"}

    total = info.samples * info.lines * info.bands
    if total == 0:
        return {"stats_error": "the header declares zero pixels"}
    multi = info.bands > 1 if per_band is None else per_band
    if total > max_pixels:
        return {
            "stats_error": (
                f"dataset has {total} pixels, above the {max_pixels} safety limit for a "
                "statistics pass; use the RasterStatistics ENVITask for large rasters"
            ),
            "sample_count": total,
        }

    try:
        blob = data_path.read_bytes()
    except OSError as exc:
        return {"stats_error": f"cannot read {data_path}: {exc}"}
    blob = blob[info.header_offset :]
    count = len(blob) // element
    if count == 0:
        return {"stats_error": f"{data_path} holds no complete pixels"}
    endian = ">" if info.byte_order else "<"
    values = array.array(struct_code)
    values.frombytes(blob[: count * element])
    if struct.pack(endian + struct_code, 1) != struct.pack("=" + struct_code, 1):
        values.byteswap()

    out: dict[str, Any] = {
        "sample_count": info.samples * info.lines * info.bands,
        "read_count": count,
        "per_band": multi,
    }
    # ENVI stores BSQ (band sequential) by default, which is the layout this
    # assumes when splitting by band. BIP/BIL would interleave differently, so
    # band-splitting is only attempted for BSQ; otherwise the whole raster is
    # reduced as one population.
    if not multi or info.interleave != "bsq" or count % info.bands != 0:
        out["stats_error"] = (
            None
            if not multi
            else f"per-band statistics need BSQ layout; this dataset is {info.interleave}"
        )
        if out["stats_error"] is None:
            out.pop("stats_error")
        out.update(_reduce(values, ""))
        return out

    per = count // info.bands
    for b in range(info.bands):
        out.update(_reduce(values[b * per : (b + 1) * per], f"_{b + 1}"))
    return out


def _reduce(values: "array.array[Any]", label: str) -> dict[str, Any]:
    """min/max/mean/stddev of one band, keyed with an optional band suffix."""
    n = len(values)
    if n == 0:
        return {}
    lo = min(values)
    hi = max(values)
    total = 0.0
    for v in values:
        total += v
    mean = total / n
    variance = 0.0
    if n > 1:
        acc = 0.0
        for v in values:
            d = v - mean
            acc += d * d
        variance = acc / (n - 1)
    return {
        f"min{label}": float(lo),
        f"max{label}": float(hi),
        f"mean{label}": float(mean),
        f"stddev{label}": math.sqrt(variance),
        f"finite_count{label}": n,
    }


def cog_backend() -> dict[str, Any]:
    """Which optional library could write a Cloud-Optimized GeoTIFF, if any."""
    found: dict[str, Any] = {"available": False, "modules": []}
    for name in ("osgeo.gdal", "rasterio"):
        try:
            if importlib.util.find_spec(name) is not None:
                found["modules"].append(name)
        except (ImportError, ValueError):  # pragma: no cover - broken install
            continue
    found["available"] = bool(found["modules"])
    return found


def export_cog(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Copy/warp a dataset to a Cloud-Optimized GeoTIFF.

    ENVI 5.2 cannot write COG, so this needs GDAL (``gdal_translate -of COG``)
    or Rasterio. Both are optional extras; when neither is importable the caller
    gets an explicit ``missing_dependency`` answer rather than a fake success.
    """
    src = Path(source).expanduser()
    dst = Path(destination).expanduser()
    if not src.exists():
        raise RasterError(f"source dataset not found: {src}")

    backend = cog_backend()
    if "osgeo.gdal" in backend["modules"]:
        from osgeo import gdal  # type: ignore

        gdal.UseExceptions()
        options = gdal.TranslateOptions(format="COG")
        gdal.Translate(str(dst), str(src), options=options)
        return {"ok": True, "backend": "gdal", "path": str(dst), "bytes": _size(dst)}

    if "rasterio" in backend["modules"]:
        import rasterio  # type: ignore
        from rasterio.shutil import copy as rio_copy

        with rasterio.open(src) as dataset:
            rio_copy(dataset, str(dst), driver="COG")
        return {"ok": True, "backend": "rasterio", "path": str(dst), "bytes": _size(dst)}

    return {
        "ok": False,
        "error": "missing_dependency",
        "message": (
            "Cloud-Optimized GeoTIFF needs GDAL or Rasterio, and neither is installed in this "
            "environment. Install the optional extra (pip install -e \".[geo]\") or ask ENVI for "
            "a plain GeoTIFF instead -- ENVI 5.2's ENVIRaster::Export has no COG writer."
        ),
        "would_write": str(dst),
    }


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover
        return 0
