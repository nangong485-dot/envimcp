"""Task catalog: read the ENVI ``*.task`` specifications that define every ENVITask.

Verified facts about the 58 templates shipped with ENVI 5.2
(``D:\\ENVI52\\resource\\templates\\tasks``):

* every ``invocationType`` is ``"keywords"``
* each parameter carries ``name`` (the documented property, e.g. ``INPUT_RASTER``)
  and ``keyword`` (the legacy routine keyword, e.g. ``TARGET``); they differ for
  ~180 parameters, and there are **no collisions** between keywords of one task,
  so generated jobs set properties by ``name`` while keeping ``keyword`` for
  diagnostics
* ``parameterType`` is ``required`` (175) or ``optional`` (125)
* ``choiceList`` is either a JSON array of strings (``SpectralIndex.INDEX``) or a
  JSON object mapping the user-facing name to an internal integer
  (``RadiometricCalibration.CALIBRATION_TYPE`` = ``{"Radiance": 0, ...}``)
* ``dataType`` ranges over scalars (``DOUBLE``, ``FLOAT``, ``INT``, ``UINT``,
  ``LONG``, ``ULONG``, ``ULONG64``, ``BOOL``, ``BYTE``, ``STRING``), dimensioned
  arrays (``DOUBLE[*]``, ``DOUBLE[2, *]``, ``DOUBLE[*, *]``, ``BYTE[3, *]``,
  ``DOUBLE[2]``, ``INT[4]``, the ``DOUBLE[*,*]`` spelling variant) and ENVI
  object types (``ENVIRASTER``, ``ENVIRASTER[*]``, ``ENVIROI[*]``, ``ENVIVECTOR``,
  ``ENVIGCPSET``, ``ENVIRASTERSERIES``, ``ENVIURI``, ``ENVIURI[*]``,
  ``ENVIGRIDDEFINITION``, plus the internal ``_enviSpatialRef``).

The catalog is therefore built from the files, not from prose documentation.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "ParameterSpec",
    "TaskSpec",
    "TaskCatalog",
    "CatalogError",
    "UnknownTaskError",
    "load_catalog",
    "KNOWN_TASK_ALIASES",
]

#: Legacy/alternative spellings seen in ENVI prose documentation mapped onto the
#: template name. Kept tiny and explicit; resolution also falls back to
#: case-insensitive matching plus difflib suggestions.
KNOWN_TASK_ALIASES: dict[str, str] = {
    "darksubtraction": "DarkSubtractionCorrection",
    "autochangethresholdclassificationtask": "AutoChangeThresholdClassification",
    "changethresholdclassification": "ChangeThresholdClassification",
    "spectralindices": "SpectralIndex",
    "spectralindex": "SpectralIndices",
    "quac": "QUAC",
}


class CatalogError(RuntimeError):
    """The task template directory is missing or unreadable."""


class UnknownTaskError(KeyError):
    """A requested task name is not in the catalog.

    ``candidates`` carries close matches so a caller (or an agent) can recover
    instead of guessing again.
    """

    def __init__(self, name: str, candidates: Iterable[str] = ()) -> None:
        self.name = name
        self.candidates = list(candidates)
        detail = f"unknown ENVITask {name!r}"
        if self.candidates:
            detail += "; did you mean: " + ", ".join(self.candidates[:5]) + "?"
        super().__init__(detail)


@dataclass(frozen=True)
class ParameterSpec:
    """One parameter of an ENVITask."""

    name: str
    keyword: str
    display_name: str
    data_type: str
    direction: str  # "input" | "output"
    parameter_type: str  # "required" | "optional"
    description: str = ""
    choices: tuple[str, ...] = ()
    choice_values: tuple[Any, ...] = ()
    default: Any = None
    has_choice_list: bool = False
    has_default: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ---- derived -------------------------------------------------------
    @property
    def is_input(self) -> bool:
        return self.direction == "input"

    @property
    def is_output(self) -> bool:
        return self.direction == "output"

    @property
    def is_required(self) -> bool:
        return self.parameter_type == "required"

    @property
    def is_optional(self) -> bool:
        return self.parameter_type != "required"

    @property
    def is_raster(self) -> bool:
        return self.data_type.startswith("ENVIRASTER")

    @property
    def is_uri(self) -> bool:
        return self.data_type == "ENVIURI"

    @property
    def is_object(self) -> bool:
        """True for ENVI handles that must be created, not referenced by path."""
        return self.data_type in {
            "ENVIGCPSET",
            "ENVIVECTOR",
            "ENVIROI",
            "ENVIROI[*]",
            "ENVIGRIDDEFINITION",
            "_enviSpatialRef",
        }

    @property
    def is_array(self) -> bool:
        return "[" in self.data_type

    @property
    def base_type(self) -> str:
        return self.data_type.split("[", 1)[0].strip()

    @property
    def shape(self) -> str:
        return self.data_type.split("[", 1)[1].rstrip("]") if self.is_array else ""

    def default_or_none(self) -> Any:
        return self.default if self.has_default else None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "keyword": self.keyword,
            "display_name": self.display_name,
            "data_type": self.data_type,
            "direction": self.direction,
            "parameter_type": self.parameter_type,
            "description": self.description,
        }
        if self.has_choice_list:
            out["choices"] = list(self.choices)
        if self.has_default:
            out["default"] = self.default
        return out


@dataclass
class TaskSpec:
    """One parsed ``*.task`` file."""

    name: str
    display_name: str
    description: str
    version: str
    invocation_type: str
    routine: str
    parameters: tuple[ParameterSpec, ...]
    source_path: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ---- lookups -------------------------------------------------------
    @property
    def inputs(self) -> tuple[ParameterSpec, ...]:
        return tuple(p for p in self.parameters if p.is_input)

    @property
    def outputs(self) -> tuple[ParameterSpec, ...]:
        return tuple(p for p in self.parameters if p.is_output)

    @property
    def required_inputs(self) -> tuple[ParameterSpec, ...]:
        return tuple(p for p in self.inputs if p.is_required)

    @property
    def optional_inputs(self) -> tuple[ParameterSpec, ...]:
        return tuple(p for p in self.inputs if p.is_optional)

    def get(self, name: str) -> ParameterSpec | None:
        """Case-insensitive parameter lookup (accepts the keyword too)."""
        wanted = name.strip().upper()
        for p in self.parameters:
            if p.name.upper() == wanted or p.keyword.upper() == wanted:
                return p
        return None

    def output_for(self, name: str | None = None) -> ParameterSpec | None:
        """The primary output parameter.

        With *name* given, resolve it; otherwise prefer ``OUTPUT_RASTER``, then
        the first output whose data type is an ENVIRASTER (or raster series),
        then simply the first output.
        """
        if name:
            spec = self.get(name)
            return spec if spec is not None and spec.is_output else None
        for candidate in ("OUTPUT_RASTER", "OUTPUT_ROI", "OUTPUT_VECTOR", "OUTPUT_RASTERSERIES"):
            spec = self.get(candidate)
            if spec is not None and spec.is_output:
                return spec
        for spec in self.outputs:
            if spec.data_type.startswith("ENVIRASTER"):
                return spec
        return self.outputs[0] if self.outputs else None

    def to_dict(self, *, include_parameters: bool = False) -> dict[str, Any]:
        out = {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "input_count": len(self.inputs),
            "optional_input_count": len(self.optional_inputs),
            "output_count": len(self.outputs),
        }
        if include_parameters:
            out["parameters"] = [p.to_dict() for p in self.parameters]
        return out

    def summary_line(self) -> str:
        return f"{self.name} -- {self.display_name}: {self.description}"


@dataclass
class TaskCatalog:
    """All known tasks, keyed by canonical name, with alias resolution."""

    tasks: dict[str, TaskSpec]
    template_dir: str
    #: canonical name -> set of lower-cased aliases
    _aliases: dict[str, str] = field(default_factory=dict, repr=False)
    #: diagnostics collected while loading (unreadable/duplicate templates)
    load_failures: list[str] = field(default_factory=list, repr=False)

    # ---- membership ----------------------------------------------------
    def __len__(self) -> int:
        return len(self.tasks)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.resolve(name, strict=False) is not None

    def __iter__(self):
        return iter(self.tasks.values())

    @property
    def names(self) -> list[str]:
        return sorted(self.tasks)

    # ---- resolution ----------------------------------------------------
    def resolve(self, name: str, *, strict: bool = True) -> TaskSpec | None:
        """Resolve a task name, an alias, the ``*.task`` filename or a path.

        Raises :class:`UnknownTaskError` with near-miss candidates when *strict*
        and nothing matches.
        """
        if not isinstance(name, str) or not name.strip():
            if strict:
                raise UnknownTaskError(str(name), self.names[:5])
            return None
        raw = name.strip()
        # 1. exact / case-insensitive canonical or alias
        spec = self._resolve_exact(raw)
        if spec is not None:
            return spec
        # 2. a filesystem path or a bare filename such as "SpectralIndex.task"
        candidate_paths: list[Path] = []
        if any(sep in raw for sep in ("/", "\\")):
            candidate_paths.append(Path(raw))
        if raw.lower().endswith(".task"):
            candidate_paths.append(Path(self.template_dir) / Path(raw).name)
        for path in candidate_paths:
            if path.is_file():
                spec = _parse_task_file(path)
                self.tasks[spec.name] = spec
                self._register_aliases(spec)
                return spec
        # 3. alias table (documentation spellings)
        alias = KNOWN_TASK_ALIASES.get(raw.lower().replace(" ", ""))
        if alias and alias in self.tasks:
            return self.tasks[alias]
        if strict:
            raise UnknownTaskError(raw, self.suggest(raw))
        return None

    def _resolve_exact(self, raw: str) -> TaskSpec | None:
        if raw in self.tasks:
            return self.tasks[raw]
        low = raw.lower()
        for spec in self.tasks.values():
            if spec.name.lower() == low:
                return spec
        target = self._aliases.get(low)
        if target:
            return self.tasks.get(target)
        return None

    def suggest(self, raw: str, limit: int = 5) -> list[str]:
        pool = sorted(set(self.names) | set(self._aliases))
        return difflib.get_close_matches(raw, pool, n=limit, cutoff=0.4)

    def search(self, query: str) -> list[TaskSpec]:
        """Rank tasks against a free-text query (name, display name, description)."""
        if not query or not query.strip():
            return list(self)
        q = query.strip().lower()
        scored: list[tuple[float, str, TaskSpec]] = []
        for spec in self.tasks.values():
            name = spec.name.lower()
            display = spec.display_name.lower()
            desc = spec.description.lower()
            score = 0.0
            if name == q:
                score = 100.0
            elif name.startswith(q):
                score = 80.0
            elif q in name:
                score = 60.0
            elif q in display:
                score = 40.0
            elif q in desc:
                score = 20.0
            else:
                ratio = difflib.SequenceMatcher(None, q, name).ratio()
                if ratio > 0.5:
                    score = ratio * 10.0
            if score:
                scored.append((score, spec.name, spec))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [spec for _, _, spec in scored]

    # ---- construction --------------------------------------------------
    def _register_aliases(self, spec: TaskSpec) -> None:
        self._aliases[spec.name.lower()] = spec.name
        self._aliases[Path(spec.source_path).stem.lower()] = spec.name
        collapsed = spec.name.replace(" ", "").lower()
        self._aliases.setdefault(collapsed, spec.name)

    def register_alias(self, alias: str, canonical: str) -> None:
        if canonical not in self.tasks:
            raise UnknownTaskError(canonical, self.names[:5])
        self._aliases[alias.strip().lower()] = canonical


def _parse_choice_list(raw: Any) -> tuple[tuple[str, ...], tuple[Any, ...], bool]:
    """Normalise both observed ``choiceList`` shapes.

    Returns ``(names, values, present)``. For an object mapping the names are the
    keys (user-facing) and the values are the internal integers; for an array the
    names are the entries and the values equal the names.
    """
    if raw is None:
        return (), (), False
    if isinstance(raw, dict):
        names = tuple(str(k) for k in raw)
        values = tuple(raw[k] for k in raw)
        return names, values, True
    if isinstance(raw, (list, tuple)):
        names = tuple(str(v) for v in raw)
        return names, names, True
    # Unexpected shape: keep the scalar so validation can still report it.
    return (str(raw),), (raw,), True


def _parse_task_file(path: Path) -> TaskSpec:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid JSON in task template {path}: {exc}") from exc
    except OSError as exc:
        raise CatalogError(f"cannot read task template {path}: {exc}") from exc

    parameters: list[ParameterSpec] = []
    for raw_param in data.get("parameters", []) or []:
        names, values, has_choices = _parse_choice_list(raw_param.get("choiceList"))
        has_default = "defaultValue" in raw_param
        parameters.append(
            ParameterSpec(
                name=str(raw_param.get("name", "")).strip(),
                keyword=str(raw_param.get("keyword", raw_param.get("name", ""))).strip(),
                display_name=str(raw_param.get("displayName", raw_param.get("name", ""))),
                data_type=str(raw_param.get("dataType", "STRING")).strip(),
                direction=str(raw_param.get("direction", "input")).strip().lower(),
                parameter_type=str(raw_param.get("parameterType", "optional")).strip().lower(),
                description=str(raw_param.get("description", "") or ""),
                choices=names,
                choice_values=values,
                default=raw_param.get("defaultValue"),
                has_choice_list=has_choices,
                has_default=has_default,
                raw=raw_param,
            )
        )

    name = str(data.get("name") or path.stem)
    return TaskSpec(
        name=name,
        display_name=str(data.get("displayName") or name),
        description=str(data.get("description") or ""),
        version=str(data.get("version") or ""),
        invocation_type=str(data.get("invocationType") or "keywords"),
        routine=str(data.get("routine") or ""),
        parameters=tuple(parameters),
        source_path=str(path),
        raw=data,
    )


def load_catalog(template_dir: str | Path) -> TaskCatalog:
    """Parse every ``*.task`` file in *template_dir*."""
    directory = Path(template_dir)
    if not directory.is_dir():
        raise CatalogError(
            f"ENVI task template directory not found: {directory} "
            "(set envi.task_template_dir or ENVIMCP_ENVI_TASK_TEMPLATE_DIR)"
        )
    files = sorted(directory.glob("*.task"))
    if not files:
        raise CatalogError(f"no *.task files under {directory}")

    tasks: dict[str, TaskSpec] = {}
    alias_map: dict[str, str] = {}
    failures: list[str] = []
    for file in files:
        try:
            spec = _parse_task_file(file)
        except CatalogError as exc:
            failures.append(str(exc))
            continue
        if spec.name in tasks:
            # Duplicate canonical names can only come from stray copies; keep the
            # first and let the caller see the collision in the diagnostics.
            failures.append(f"duplicate task name {spec.name!r} in {file}")
            continue
        tasks[spec.name] = spec
        alias_map[spec.name.lower()] = spec.name
        alias_map[file.stem.lower()] = spec.name
        alias_map.setdefault(spec.name.replace(" ", "").lower(), spec.name)

    if not tasks:
        raise CatalogError(
            f"no usable *.task templates under {directory}: " + "; ".join(failures[:3])
        )
    for alias, canonical in KNOWN_TASK_ALIASES.items():
        if canonical in tasks:
            alias_map.setdefault(alias, canonical)

    catalog = TaskCatalog(
        tasks=tasks, template_dir=str(directory), _aliases=alias_map, load_failures=failures
    )
    return catalog


@lru_cache(maxsize=4)
def _cached_catalog(template_dir: str) -> TaskCatalog:
    return load_catalog(template_dir)


def cached_catalog(template_dir: str | Path) -> TaskCatalog:
    """Process-wide catalog, reloaded only when the directory string changes."""
    return _cached_catalog(str(template_dir))
