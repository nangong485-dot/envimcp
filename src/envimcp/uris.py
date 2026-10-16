"""Output-path resolution with the token syntax borrowed from NV5's ENVI agent.

An agent should never have to invent a long absolute path just to ask for a
result, so output locations accept a one-character prefix:

====================  ==================================================================
``#name``             persistent: ``<outputs_dir>/name.dat`` (survives the job)
``!name``             temporary: ``<jobs/<id>>/name.dat`` (kept for inspection, cleaned up)
``*name``             virtual: no file at all; only valid as a workflow intermediate
``**``                every pixel in the virtual raster (workflow chains)
no prefix             an explicit path, which must be absolute and, unless
                      ``security.allow_outputs_outside_root`` is set, inside
                      ``outputs_dir`` or ``jobs_dir``
====================  ==================================================================

The extension defaults to ``.dat`` (ENVI) when the name has none; ``.tif`` and
``.geotiff`` are passed through so the ENVI writer emits GeoTIFF instead of the
classic ``.dat``/``.hdr`` pair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

__all__ = [
    "UriError",
    "ResolvedUri",
    "VIRTUAL_PREFIX",
    "TEMP_PREFIX",
    "PERSISTENT_PREFIX",
    "ALL_PIXELS",
    "is_virtual",
    "resolve_output",
    "check_not_overwriting_input",
]

VIRTUAL_PREFIX = "*"
TEMP_PREFIX = "!"
PERSISTENT_PREFIX = "#"
ALL_PIXELS = "**"

DEFAULT_EXTENSION = ".dat"
_KNOWN_EXTENSIONS = {".dat", ".tif", ".tiff", ".geotiff", ".gtiff", ".img", ".envi"}

#: Characters Windows rejects in a file name.
_ILLEGAL_NAME_CHARS = set('<>:"/\\|?*')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class UriError(ValueError):
    """Raised when an output URI/token cannot be resolved to a safe location."""

    def __init__(self, message: str, kind: str = "UriError") -> None:
        self.message = message
        self.kind = kind
        super().__init__(message)

    def to_dict(self) -> dict[str, object]:
        return {"error": self.kind, "message": self.message}


@dataclass(frozen=True)
class ResolvedUri:
    """Outcome of resolving an output token or path."""

    #: ``virtual`` | ``temp`` | ``persistent`` | ``path``
    kind: str
    #: Name as written by the caller, without the token and without the extension.
    name: str
    #: Absolute path, or ``None`` for virtual outputs.
    path: Path | None
    #: Original input string.
    raw: str

    @property
    def is_virtual(self) -> bool:
        return self.kind == "virtual"

    def absolute(self) -> str:
        if self.path is None:
            raise UriError(f"{self.raw!r} is a virtual output and has no path", "VirtualUri")
        return str(self.path)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "path": str(self.path) if self.path is not None else None,
            "raw": self.raw,
        }


def is_virtual(value: str) -> bool:
    return isinstance(value, str) and value.startswith(VIRTUAL_PREFIX)


def _sanitize_name(name: str, raw: str) -> str:
    text = name.strip()
    if not text:
        raise UriError(f"output name is empty in {raw!r}", "InvalidOutputName")
    if ALL_PIXELS in text:
        raise UriError(
            f"{raw!r} looks like a virtual *raster reference; that is an input, not an output",
            "InvalidOutputName",
        )
    if re.search(r"[\\/]", text):
        # The '#'/'!'/'*' tokens name a FILE inside a directory the host chose,
        # so a separator in the name means the caller misread the contract. Fail
        # loudly instead of silently keeping only the last segment, which would
        # quietly relocate e.g. '#../../evil' to outputs/evil.
        raise UriError(
            f"output name {name!r} must be a bare file name; the '#'/'!'/'*' tokens name a "
            "file inside a directory the server picks, not a subdirectory",
            "InvalidOutputName",
        )
    stem = Path(text).stem
    if not stem:
        raise UriError(f"output name {name!r} has no file stem", "InvalidOutputName")
    bad = sorted(_ILLEGAL_NAME_CHARS & set(text))
    if bad:
        raise UriError(
            f"output name {name!r} contains illegal character(s) {''.join(bad)!r}",
            "InvalidOutputName",
        )
    if stem.upper() in _RESERVED_NAMES:
        raise UriError(f"output name {name!r} is a reserved Windows device name", "InvalidOutputName")
    if text.endswith((".", " ")):
        raise UriError(f"output name {name!r} must not end with a dot or space", "InvalidOutputName")
    return text


def _with_default_extension(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if not suffix:
        return name + DEFAULT_EXTENSION
    if suffix not in _KNOWN_EXTENSIONS:
        # An unfamiliar extension is allowed (ENVI can write others) but the
        # caller gets the same treatment as any explicit path.
        return name
    return name


def _ensure_inside(candidate: Path, roots: list[Path], raw: str) -> None:
    resolved = _normalize(candidate)
    for root in roots:
        root_resolved = _normalize(root)
        try:
            resolved.relative_to(root_resolved)
            return
        except ValueError:
            continue
    allowed = ", ".join(str(r) for r in roots)
    raise UriError(
        f"output path {raw!r} resolves outside the allowed roots ({allowed}). "
        "Use a '#name' token for outputs/, a '!name' token for the job directory, "
        "or set security.allow_outputs_outside_root = true to permit absolute paths.",
        "OutputOutsideRoot",
    )


def _normalize(path: Path) -> Path:
    """Absolute, resolved, case-folded for comparison purposes."""
    try:
        absolute = path.expanduser()
        if not absolute.is_absolute():
            absolute = Path.cwd() / absolute
    except (RuntimeError, OSError):  # pragma: no cover - defensive
        absolute = path
    # Path.resolve() would fail on a missing file on some platforms; normalise
    # lexically instead and let the job create the parent directories.
    parts: list[str] = []
    for part in absolute.parts:
        if part == "..":
            if parts and parts[-1] not in ("\\", "/"):
                parts.pop()
            continue
        if part == ".":
            continue
        parts.append(part)
    normalized = Path(*parts) if parts else absolute
    return Path(str(normalized).replace("\\", "/").rstrip("/"))


def resolve_output(
    raw: str,
    *,
    config: Config,
    job_dir: Path,
    output_dir: Path | None = None,
) -> ResolvedUri:
    """Resolve one output specification.

    *output_dir* overrides ``config.output_root_path`` for the ``#`` token (used
    when a caller pins a batch destination).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UriError("output path must be a non-empty string", "InvalidOutputName")

    text = raw.strip()
    persistent_root = Path(output_dir) if output_dir is not None else config.output_root_path

    if text.startswith(VIRTUAL_PREFIX):
        if text == ALL_PIXELS:
            raise UriError("'**' is only valid as an input reference", "VirtualUri")
        name = _sanitize_name(text[1:], raw)
        return ResolvedUri(kind="virtual", name=name, path=None, raw=raw)

    if text.startswith(TEMP_PREFIX):
        name = _with_default_extension(_sanitize_name(text[1:], raw))
        return ResolvedUri(kind="temp", name=name, path=Path(job_dir) / name, raw=raw)

    if text.startswith(PERSISTENT_PREFIX):
        name = _with_default_extension(_sanitize_name(text[1:], raw))
        return ResolvedUri(kind="persistent", name=name, path=persistent_root / name, raw=raw)

    candidate = Path(text)
    if not candidate.is_absolute():
        raise UriError(
            f"output path {raw!r} is relative; give an absolute path or use a token "
            "(#name for outputs/, !name for the job directory)",
            "RelativeOutputPath",
        )
    if not config.security.allow_outputs_outside_root:
        _ensure_inside(candidate, [config.output_root_path, config.jobs_path, persistent_root], raw)
    return ResolvedUri(kind="path", name=candidate.name, path=candidate, raw=raw)


def check_not_overwriting_input(output: ResolvedUri, input_paths: list[str], *, context: str) -> None:
    """Refuse to write a result on top of one of its own inputs."""
    if output.path is None:
        return
    target = _normalize(output.path)
    for raw_input in input_paths:
        if not isinstance(raw_input, str) or not raw_input.strip():
            continue
        candidate = Path(raw_input.strip().lstrip(VIRTUAL_PREFIX))
        if not candidate.is_absolute():
            continue
        # ENVI datasets are referenced by stem (foo) or by foo.dat; treat both the
        # given path and its .dat/.hdr siblings as the same dataset.
        stems = {_normalize(candidate)}
        stems.add(_normalize(candidate.with_suffix("")))
        for suffix in (".dat", ".hdr", ".tif", ".tiff", ".envi"):
            stems.add(_normalize(Path(str(candidate.with_suffix("")) + suffix)))
        if target in stems:
            raise UriError(
                f"{context}: refusing to overwrite the input dataset {raw_input!r} with the "
                "task output; choose a different output path",
                "OutputOverwritesInput",
            )
