"""JSON Schema generation and input validation for ENVITask parameters.

The ``*.task`` templates are the single source of truth. Two quirks drive the
code below and are reproduced verbatim from the ENVI 5.2 templates:

* ``choiceList`` may be an array (names only) or an object (name -> internal int).
  Only the *keys* are user-facing; ``RadiometricCalibration.CALIBRATION_TYPE``
  therefore accepts ``"Radiance"``, not ``0``.
* Friendly index abbreviations are *not* in the templates: ``SpectralIndex.INDEX``
  lists ``"Normalized Difference Vegetation Index"`` and has no ``"NDVI"`` entry.
  :func:`resolve_choice` accepts a case-insensitive exact match, then the unique
  acronym match, then the unique prefix match, and otherwise fails with the
  closest real choices listed.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import replace
from typing import Any, Iterable, Mapping

from .catalog import ParameterSpec, TaskSpec

__all__ = [
    "ValidationError",
    "parameter_schema",
    "task_input_schema",
    "task_schema",
    "validate_inputs",
    "normalize_inputs",
    "resolve_choice",
    "coerce_value",
]

_ARRAY_RE = re.compile(r"^(?P<base>[A-Za-z_][A-Za-z0-9_]*)\s*\[(?P<shape>[^\]]*)\]$")

_NUMERIC_BASES = {"DOUBLE", "FLOAT", "INT", "UINT", "LONG", "ULONG", "ULONG64", "BYTE", "BOOL"}

#: ENVI object types envimcp can build from a raster path already loaded by the job.
_PATH_OBJECT_TYPES = {"ENVIRASTER", "ENVIURI", "ENVIRASTERSERIES"}

#: ENVI object types that need a real ENVI handle and cannot be given as a path.
_HANDLE_ONLY_TYPES = {
    "ENVIVECTOR": "an ENVI vector handle",
    "ENVIROI": "an ENVI ROI handle",
    "ENVIROI[*]": "a list of ENVI ROI handles",
    "ENVIGCPSET": "an ENVIGCPSet handle",
    "ENVIGRIDDEFINITION": "an ENVIGridDefinition handle",
    "_enviSpatialRef": "an ENVI spatial reference handle",
}


class ValidationError(ValueError):
    """Raised when task inputs do not satisfy the ENVI task definition.

    ``errors`` lists one message per offending parameter, so an agent can fix
    several problems in one pass instead of iterating guess-by-guess.
    """

    def __init__(self, message: str, errors: Iterable[str] = ()) -> None:
        self.errors = list(errors) or [message]
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"error": "ValidationError", "message": self.message, "errors": self.errors}


def _base_and_shape(data_type: str) -> tuple[str, str, bool]:
    m = _ARRAY_RE.match(data_type.strip())
    if m:
        return m.group("base").upper(), m.group("shape"), True
    return data_type.strip(), "", False


def _scalar_json_schema(data_type: str) -> dict[str, Any]:
    base, _, _ = _base_and_shape(data_type)
    if base == "STRING":
        return {"type": "string"}
    if base == "BOOL":
        return {"type": "boolean"}
    if base == "BYTE":
        return {"type": "integer", "minimum": 0, "maximum": 255}
    if base in {"INT", "LONG", "UINT", "ULONG", "ULONG64"}:
        return {"type": "integer"}
    if base in {"DOUBLE", "FLOAT"}:
        return {"type": "number"}
    return {"type": "string"}


def parameter_schema(param: ParameterSpec) -> dict[str, Any]:
    """JSON Schema for one parameter, annotated with ENVI metadata."""
    base, shape, is_array = _base_and_shape(param.data_type)
    schema: dict[str, Any]

    if base == "ENVIRASTER" or base == "ENVIURI" or base == "ENVIRASTERSERIES":
        schema = {"type": "string"}
        schema["format"] = "envi-raster-path" if base != "ENVIURI" else "envi-uri"
        schema["x-envi-object"] = param.data_type
    elif param.data_type in _HANDLE_ONLY_TYPES:
        schema = {"type": "object"}
        schema["x-envi-handle-only"] = _HANDLE_ONLY_TYPES[param.data_type]
    elif is_array:
        item = _scalar_json_schema(base)
        if param.has_choice_list and base == "STRING":
            item = {"type": "string", "enum": list(param.choices)}
        schema = {"type": "array", "items": item}
        fixed = _fixed_shape(shape)
        if fixed is not None:
            schema["minItems"] = fixed
            schema["maxItems"] = fixed
            schema["x-envi-shape"] = shape
        if base == "BYTE" and shape.replace(" ", "") == "3,*":
            schema["x-envi-role"] = "per-class RGB triples (3 rows x N classes)"
    else:
        schema = _scalar_json_schema(param.data_type)

    if param.has_choice_list and not is_array:
        schema["enum"] = list(param.choices)
        schema["x-envi-choice-values"] = list(param.choice_values)
        if base == "STRING" and param.choices and param.choices[0][:1].isdigit():
            schema = {"type": schema["type"], "enum": schema["enum"]}

    if param.has_default:
        schema["default"] = param.default

    schema["title"] = param.display_name or param.name
    if param.description:
        schema["description"] = param.description.strip()
    schema["x-envi-keyword"] = param.keyword
    schema["x-envi-data-type"] = param.data_type
    if param.has_choice_list:
        schema["x-envi-choice-hint"] = (
            "Accepted case-insensitively; a unique acronym (e.g. NDVI) or unique "
            "prefix also resolves to the full choice name."
        )
    return schema


def _fixed_shape(shape: str) -> int | None:
    """Return N when the array shape is exactly ``[N]``, else None."""
    compact = shape.replace(" ", "")
    if "," in compact:
        return None
    if compact.isdigit():
        return int(compact)
    return None


def task_input_schema(spec: TaskSpec) -> dict[str, Any]:
    """JSON Schema object covering only the *input* parameters of a task."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in spec.inputs:
        properties[param.name] = parameter_schema(param)
        if param.is_required:
            required.append(param.name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        "title": f"{spec.display_name} inputs",
        "description": spec.description.strip(),
    }
    if required:
        schema["required"] = required
    return schema


def task_schema(spec: TaskSpec, *, output_as_path: bool = True) -> dict[str, Any]:
    """Full tool-facing schema: call arguments for ``envi_run_task``-style entry.

    This is what ``envi_get_task_schema`` returns: the task's inputs plus the
    chosen output parameter rendered as a *path string*, plus the shared run
    options, so an agent can build a complete tool call from it.
    """
    inputs = task_input_schema(spec)
    properties: dict[str, Any] = dict(inputs["properties"])
    required = list(inputs.get("required", []))

    output_param = spec.output_for()
    output_key: str | None = None
    if output_param is not None and output_as_path:
        output_key = output_param.name
        out_schema = parameter_schema(output_param)
        out_schema["type"] = "string"
        out_schema.pop("x-envi-object", None)
        out_schema["x-envi-output"] = True
        out_schema["description"] = (
            (output_param.description.strip() + " " if output_param.description else "")
            + "Give a filesystem path (.dat for ENVI, .tif/.geotiff for GeoTIFF). "
            "Prefix with '#' for outputs/<name>, '!' for a file inside the job "
            "directory, or '*' to keep it virtual (workflows only)."
        )
        properties[output_key] = out_schema
        required.append(output_key)

    properties["output"] = {
        "type": "string",
        "description": (
            "Optional shorthand for the output path. Use this instead of "
            f"{output_key!r} when you only need one output."
            if output_key
            else "Optional output path override."
        ),
    }
    properties["timeout_s"] = {"type": "integer", "minimum": 1, "description": "Wall-clock budget."}
    properties["export"] = {
        "type": "object",
        "properties": {"cog": {"type": "boolean"}, "gtiff": {"type": "boolean"}},
        "additionalProperties": False,
        "description": "Optionally also export the result (needs the optional [geo] extra).",
    }

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
        "title": f"{spec.display_name} ({spec.name})",
        "description": (
            f"ENVI task {spec.name}. {spec.description.strip()} "
            f"Set 'inputs' to an object with these parameters, or pass them at the top level."
        ),
    }
    schema["x-envi-task"] = spec.name
    schema["x-envi-task-version"] = spec.version
    schema["x-envi-output-parameter"] = output_key
    schema["x-envi-parameter-count"] = len(spec.parameters)
    if output_key:
        schema["x-envi-output-hint"] = (
            f"The result raster is reported back as outputs.{output_key} together with "
            "raster_info and stats."
        )
    return schema


def resolve_choice(param: ParameterSpec, value: Any) -> Any:
    """Map a user-supplied choice onto the canonical name listed in the template."""
    if not param.has_choice_list or not param.choices:
        return value
    if not isinstance(value, str):
        # An internal integer is accepted only when it matches a choice value.
        for name, internal in zip(param.choices, param.choice_values):
            if value == internal:
                return name
        raise ValidationError(
            f"{param.name}: expected one of {list(param.choices)!r}, got {value!r}",
            [f"{param.name}: expected a choice name, got {value!r}"],
        )

    text = value.strip()
    lowered = text.lower()

    # 1. exact (case-insensitive)
    for name in param.choices:
        if name.lower() == lowered:
            return name

    # 2. unique acronym match -- "NDVI" -> "Normalized Difference Vegetation Index"
    acronym = _acronym(text)
    matches = [n for n in param.choices if _acronym(n) == acronym]
    if len(matches) == 1:
        return matches[0]

    # 3. unique prefix / substring match
    matches = [n for n in param.choices if n.lower().startswith(lowered)]
    if len(matches) != 1:
        contained = [n for n in param.choices if lowered in n.lower()]
        if len(contained) == 1:
            matches = contained
    if len(matches) == 1:
        return matches[0]

    close = difflib.get_close_matches(text, list(param.choices), n=5, cutoff=0.3)
    hint = f"; closest choices: {close}" if close else ""
    raise ValidationError(
        f"{param.name}: {value!r} is not a valid choice{hint}",
        [f"{param.name}: {value!r} is not a valid choice{hint}"],
    )


_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _acronym(text: str) -> str:
    """Initials of the significant words: 'Normalized Difference Vegetation Index' -> ndvi."""
    words = [w for w in _SPLIT_RE.split(text) if w]
    if not words:
        return ""
    if len(words) == 1:
        return words[0].lower()
    return "".join(w[0].lower() for w in words)


def coerce_value(param: ParameterSpec, value: Any) -> Any:
    """Coerce a JSON value into the Python shape the IDL writer expects."""
    if value is None:
        return None

    base, shape, is_array = _base_and_shape(param.data_type)

    if base == "BOOL" and not is_array:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in {"true", "yes", "1", "on"}:
                return True
            if low in {"false", "no", "0", "off"}:
                return False
        raise ValidationError(
            f"{param.name}: expected a boolean, got {value!r}",
            [f"{param.name}: expected a boolean, got {value!r}"],
        )

    if is_array:
        fixed = _fixed_shape(shape)
        if not isinstance(value, (list, tuple)):
            # ENVI accepts a scalar wherever it accepts an array -- its own help for
            # e.g. LinearPercentStretchRaster.PERCENT says "a double-precision
            # floating point value, or array of values" -- so a scalar promotes to a
            # one-element array whenever the shape is open-ended ("*"). A fixed
            # shape such as [2] or [3,*] keeps its length as part of the contract
            # and still rejects a bare scalar.
            if base == "STRING" or "*" in shape:
                value = [value]
            else:
                raise ValidationError(
                    f"{param.name}: expected an array of exactly {shape} values "
                    f"({param.data_type}), got {type(value).__name__}",
                    [f"{param.name}: expected {shape} values, got a scalar"],
                )
        elif fixed is not None and len(value) != fixed:
            raise ValidationError(
                f"{param.name}: expected {fixed} values ({param.data_type}), got {len(value)}",
                [f"{param.name}: expected {fixed} values, got {len(value)}"],
            )
        items = [coerce_value(_as_scalar(param, base), v) for v in value]
        return items

    if base == "STRING":
        return str(value)
    if base in {"DOUBLE", "FLOAT"}:
        return _to_number(param, value)
    if base in {"INT", "LONG", "UINT", "ULONG", "ULONG64", "BYTE"}:
        number = _to_number(param, value)
        if float(number) != int(number):
            raise ValidationError(
                f"{param.name}: expected an integer, got {value!r}",
                [f"{param.name}: expected an integer, got {value!r}"],
            )
        return int(number)
    return value


def _as_scalar(param: ParameterSpec, base: str) -> ParameterSpec:
    """A scalar view of an array parameter, used to reuse :func:`coerce_value`."""
    return replace(param, data_type=base, choices=(), choice_values=(), has_choice_list=False)


def _to_number(param: ParameterSpec, value: Any) -> float:
    if isinstance(value, bool):
        raise ValidationError(
            f"{param.name}: expected a number, got {value!r}",
            [f"{param.name}: expected a number, got {value!r}"],
        )
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ValidationError(
        f"{param.name}: expected a number, got {value!r}",
        [f"{param.name}: expected a number, got {value!r}"],
    )


def normalize_inputs(spec: TaskSpec, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise an ``inputs`` mapping for *spec*.

    Unknown keys raise; parameter lookup is case-insensitive and accepts the
    legacy ``keyword`` spelling. Returns canonical ``{PARAM_NAME: value}`` with
    choices resolved and scalars coerced.
    """
    if not isinstance(inputs, Mapping):
        raise ValidationError(
            "inputs must be an object mapping parameter names to values",
            ["inputs must be an object"],
        )

    errors: list[str] = []
    normalized: dict[str, Any] = {}
    seen: set[str] = set()

    for raw_key, raw_value in inputs.items():
        param = spec.get(str(raw_key))
        if param is None:
            close = difflib.get_close_matches(str(raw_key).upper(), [p.name for p in spec.parameters], n=3)
            hint = f"; did you mean {close}?" if close else ""
            errors.append(f"unknown parameter {raw_key!r} for task {spec.name}{hint}")
            continue
        if param.is_output:
            errors.append(
                f"{param.name} is an output parameter; pass it via 'output'/'{param.name}' "
                "at the top level instead of in inputs"
            )
            continue
        if param.name in seen:
            errors.append(f"{param.name} was supplied more than once")
            continue
        seen.add(param.name)

        try:
            normalized[param.name] = _normalize_one(param, raw_value)
        except ValidationError as exc:
            errors.extend(exc.errors)

    for param in spec.required_inputs:
        if param.name not in normalized:
            errors.append(f"missing required parameter {param.name} ({param.display_name})")
        elif normalized[param.name] is None:
            errors.append(f"required parameter {param.name} is null")

    if errors:
        raise ValidationError(
            f"{len(errors)} problem(s) with inputs for task {spec.name}: " + "; ".join(errors),
            errors,
        )
    return normalized


def validate_inputs(spec: TaskSpec, inputs: Mapping[str, Any]) -> list[str]:
    """Return a list of problems (empty means valid); never raises."""
    try:
        normalize_inputs(spec, inputs)
    except ValidationError as exc:
        return list(exc.errors)
    return []


def _normalize_one(param: ParameterSpec, value: Any) -> Any:
    if value is None:
        return None
    base, _, _ = _base_and_shape(param.data_type)

    if param.data_type in _HANDLE_ONLY_TYPES:
        raise ValidationError(
            f"{param.name} needs {_HANDLE_ONLY_TYPES[param.data_type]}, which cannot be "
            "supplied as JSON; build it in a custom IDL step or use a task that accepts "
            "a raster path",
            [
                f"{param.name}: {param.data_type} inputs are not supported over MCP "
                "(handle-only type)"
            ],
        )

    if base == "ENVIRASTER" or base == "ENVIURI" or base == "ENVIRASTERSERIES":
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                f"{param.name}: expected a raster path string, got {value!r}",
                [f"{param.name}: expected a non-empty path string"],
            )
        return value.strip()

    if param.has_choice_list:
        value = resolve_choice(param, value)

    return coerce_value(param, value)
