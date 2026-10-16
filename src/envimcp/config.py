"""Configuration for envimcp.

Precedence (highest first):
  1. environment variables  ``ENVIMCP_<SECTION>_<KEY>``
  2. an explicit ``--config`` TOML file
  3. ``config/envimcp.toml`` at the project root, if present
  4. built-in ``DEFAULT_CONFIG``

The defaults describe this machine (ENVI 5.2 in ``D:\\ENVI52``, IDL 8.4 in
``D:\\IDL84``). Override them for another install instead of editing code.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):  # pragma: no cover - trivial
    import tomllib
else:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ModuleNotFoundError(
            "Python < 3.11 needs 'tomli' to read envimcp.toml (pip install tomli)"
        ) from exc

ENV_PREFIX = "ENVIMCP_"
DEFAULT_CONFIG_NAME = "envimcp.toml"

DEFAULT_CONFIG: dict[str, dict[str, Any]] = {
    "envi": {
        "root": r"D:\ENVI52",
        "task_template_dir": r"D:\ENVI52\resource\templates\tasks",
        "sample_raster": r"D:\ENVI52\data\qb_boulder_msi",
    },
    "idl": {
        "exe": r"D:\IDL84\bin\bin.x86_64\idl.exe",
        "lib_dir": "idl",
        "pref": "IDL_STARTUP=,IDL_PATH=+<IDL_DEFAULT>,IDL_DEMO_MODE=0",
    },
    "paths": {
        "jobs_dir": "jobs",
        "outputs_dir": "outputs",
        "output_root": "",
    },
    "limits": {
        "max_concurrent": 1,
        "on_busy": "queue",
        "timeout_s": 900,
        "max_retries": 2,
    },
    "server": {
        "name": "envimcp",
        "transport": "stdio",
        "host": "127.0.0.1",
        "port": 8765,
    },
    "security": {
        "allow_raw_idl": False,
        "allow_outputs_outside_root": False,
    },
}

#: Which sections exist, in file order, and the dataclass that holds each one.
SECTION_TYPES: dict[str, str] = {
    "envi": "EnviSection",
    "idl": "IdlSection",
    "paths": "PathsSection",
    "limits": "LimitsSection",
    "server": "ServerSection",
    "security": "SecuritySection",
}

_INT_KEYS = {"max_concurrent", "timeout_s", "max_retries", "port"}
_BOOL_KEYS = {"allow_raw_idl", "allow_outputs_outside_root"}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised when the effective configuration cannot be used."""


def project_root() -> Path:
    """Repository root: the directory holding ``pyproject.toml``.

    Walks up from this file so the package keeps working when installed in
    editable mode and invoked from any working directory.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    for parent in here.parents:
        if (parent / "config" / DEFAULT_CONFIG_NAME).is_file():
            return parent
    return here.parents[2] if len(here.parents) > 2 else Path.cwd()


def _coerce(value: Any, key: str, section: str) -> Any:
    """Convert an environment-variable string into the declared value type."""
    if isinstance(value, str):
        if key in _INT_KEYS:
            try:
                return int(value)
            except ValueError as exc:
                raise ConfigError(
                    f"{ENV_PREFIX}{section.upper()}_{key.upper()} must be an integer, got {value!r}"
                ) from exc
        if key in _BOOL_KEYS:
            low = value.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ConfigError(
                f"{ENV_PREFIX}{section.upper()}_{key.upper()} must be a boolean, got {value!r}"
            )
    return value


@dataclass
class EnviSection:
    root: str = field(default_factory=lambda: DEFAULT_CONFIG["envi"]["root"])
    task_template_dir: str = field(
        default_factory=lambda: DEFAULT_CONFIG["envi"]["task_template_dir"]
    )
    sample_raster: str = field(default_factory=lambda: DEFAULT_CONFIG["envi"]["sample_raster"])


@dataclass
class IdlSection:
    exe: str = field(default_factory=lambda: DEFAULT_CONFIG["idl"]["exe"])
    lib_dir: str = field(default_factory=lambda: DEFAULT_CONFIG["idl"]["lib_dir"])
    pref: str = field(default_factory=lambda: DEFAULT_CONFIG["idl"]["pref"])


@dataclass
class PathsSection:
    jobs_dir: str = field(default_factory=lambda: DEFAULT_CONFIG["paths"]["jobs_dir"])
    outputs_dir: str = field(default_factory=lambda: DEFAULT_CONFIG["paths"]["outputs_dir"])
    output_root: str = field(default_factory=lambda: DEFAULT_CONFIG["paths"]["output_root"])

    def __post_init__(self) -> None:
        # The '#' persistent token writes under output_root; default it to outputs_dir.
        if not self.output_root:
            self.output_root = self.outputs_dir


@dataclass
class LimitsSection:
    max_concurrent: int = field(default_factory=lambda: DEFAULT_CONFIG["limits"]["max_concurrent"])
    on_busy: str = field(default_factory=lambda: DEFAULT_CONFIG["limits"]["on_busy"])
    timeout_s: int = field(default_factory=lambda: DEFAULT_CONFIG["limits"]["timeout_s"])
    max_retries: int = field(default_factory=lambda: DEFAULT_CONFIG["limits"]["max_retries"])


@dataclass
class ServerSection:
    name: str = field(default_factory=lambda: DEFAULT_CONFIG["server"]["name"])
    transport: str = field(default_factory=lambda: DEFAULT_CONFIG["server"]["transport"])
    host: str = field(default_factory=lambda: DEFAULT_CONFIG["server"]["host"])
    port: int = field(default_factory=lambda: DEFAULT_CONFIG["server"]["port"])


@dataclass
class SecuritySection:
    allow_raw_idl: bool = field(
        default_factory=lambda: DEFAULT_CONFIG["security"]["allow_raw_idl"]
    )
    allow_outputs_outside_root: bool = field(
        default_factory=lambda: DEFAULT_CONFIG["security"]["allow_outputs_outside_root"]
    )


@dataclass
class Config:
    envi: EnviSection = field(default_factory=EnviSection)
    idl: IdlSection = field(default_factory=IdlSection)
    paths: PathsSection = field(default_factory=PathsSection)
    limits: LimitsSection = field(default_factory=LimitsSection)
    server: ServerSection = field(default_factory=ServerSection)
    security: SecuritySection = field(default_factory=SecuritySection)

    #: Where the TOML came from, for diagnostics.
    source: str = "<defaults>"
    #: Repository root used to resolve relative paths.
    root: Path = field(default_factory=project_root)

    # ---- derived paths -------------------------------------------------
    @property
    def jobs_path(self) -> Path:
        return self._resolve_dir(self.paths.jobs_dir)

    @property
    def outputs_path(self) -> Path:
        return self._resolve_dir(self.paths.outputs_dir)

    @property
    def output_root_path(self) -> Path:
        return self._resolve_dir(self.paths.output_root)

    @property
    def idl_lib_path(self) -> Path:
        return self._resolve_dir(self.idl.lib_dir)

    @property
    def task_template_path(self) -> Path:
        return Path(self.envi.task_template_dir)

    def _resolve_dir(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    def ensure_dirs(self) -> None:
        for p in (self.jobs_path, self.outputs_path, self.output_root_path):
            p.mkdir(parents=True, exist_ok=True)

    # ---- validation ---------------------------------------------------
    def validate(self, *, require_idl: bool = True) -> list[str]:
        """Return a list of problems; empty means the configuration is usable."""
        problems: list[str] = []
        if require_idl and not Path(self.idl.exe).is_file():
            problems.append(
                f"idl.exe not found at {self.idl.exe!r}; real ENVI tasks need a local IDL "
                "installation (set ENVIMCP_IDL_EXE or edit config/envimcp.toml)"
            )
        if not Path(self.envi.task_template_dir).is_dir():
            problems.append(
                f"ENVI task template directory not found: {self.envi.task_template_dir!r}"
            )
        elif not list(Path(self.envi.task_template_dir).glob("*.task")):
            problems.append(
                f"no *.task files under {self.envi.task_template_dir!r}; "
                "check envi.task_template_dir"
            )
        if not self.idl_lib_path.is_dir():
            problems.append(f"IDL library directory not found: {self.idl_lib_path}")
        else:
            for name in ("envi_dsh_runner.pro", "envi_dsh_util.pro"):
                if not (self.idl_lib_path / name).is_file():
                    problems.append(f"missing IDL library file: {self.idl_lib_path / name}")
        if self.limits.on_busy not in {"queue", "reject"}:
            problems.append("limits.on_busy must be 'queue' or 'reject'")
        if self.limits.max_concurrent < 1:
            problems.append("limits.max_concurrent must be >= 1")
        if self.limits.timeout_s < 1:
            problems.append("limits.timeout_s must be >= 1")
        if self.limits.max_retries < 0:
            problems.append("limits.max_retries must be >= 0")
        if self.server.transport not in {"stdio", "http"}:
            problems.append("server.transport must be 'stdio' or 'http'")
        if not (0 < self.server.port < 65536):
            problems.append("server.port must be 1..65535")
        return problems

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "root": str(self.root),
            "idl_exe": self.idl.exe,
            "envi_root": self.envi.root,
            "task_template_dir": self.envi.task_template_dir,
            "task_template_count": count_task_templates(self.envi.task_template_dir),
            "sample_raster": self.envi.sample_raster,
            "jobs_dir": str(self.jobs_path),
            "outputs_dir": str(self.outputs_path),
            "output_root": str(self.output_root_path),
            "limits": {
                "max_concurrent": self.limits.max_concurrent,
                "on_busy": self.limits.on_busy,
                "timeout_s": self.limits.timeout_s,
                "max_retries": self.limits.max_retries,
            },
            "transport": self.server.transport,
            "allow_raw_idl": self.security.allow_raw_idl,
        }


def count_task_templates(directory: str | os.PathLike[str]) -> int:
    p = Path(directory)
    if not p.is_dir():
        return 0
    return len(list(p.glob("*.task")))


def _find_default_config() -> Path | None:
    env = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env:
        return Path(env)
    candidate = project_root() / "config" / DEFAULT_CONFIG_NAME
    return candidate if candidate.is_file() else None


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _env_overrides() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        rest = key[len(ENV_PREFIX) :]
        if rest == "CONFIG" or "_" not in rest:
            continue
        section, _, name = rest.partition("_")
        out[(section.lower(), name.lower())] = value
    return out


def load_config(
    path: str | os.PathLike[str] | None = None, *, apply_env: bool = True
) -> Config:
    """Build the effective :class:`Config`."""
    data: dict[str, Any] = {}
    config_path: Path | None = Path(path) if path else _find_default_config()
    if config_path is not None and config_path.is_file():
        data = _load_toml(config_path)
        source = str(config_path)
    elif path:
        raise ConfigError(f"config file not found: {path}")
    else:
        source = "<defaults>"

    env_overrides = _env_overrides() if apply_env else {}

    section_classes = {
        "envi": EnviSection,
        "idl": IdlSection,
        "paths": PathsSection,
        "limits": LimitsSection,
        "server": ServerSection,
        "security": SecuritySection,
    }
    built: dict[str, Any] = {}
    for section, section_cls in section_classes.items():
        default_section = DEFAULT_CONFIG.get(section, {})
        file_section = data.get(section, {}) or {}
        unknown = set(file_section) - set(default_section)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [{section}] of {source}: {', '.join(sorted(unknown))}"
            )
        values: dict[str, Any] = {}
        for f in fields(section_cls):
            key = f.name
            if key in file_section:
                values[key] = file_section[key]
            elif key in default_section:
                values[key] = default_section[key]
            env_value = env_overrides.get((section, key))
            if env_value is not None:
                values[key] = _coerce(env_value, key, section)
        built[section] = section_cls(**values)

    return Config(source=source, root=project_root(), **built)


def write_default_config(target: Path) -> Path:
    """Write the reference TOML; used by tools/setup_env.ps1 on a fresh clone."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return target
    lines: list[str] = ["# Generated by envimcp.config -- edit to match your install.\n"]
    for section, values in DEFAULT_CONFIG.items():
        lines.append(f"\n[{section}]\n")
        for key, value in values.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            else:
                rendered = '"' + str(value).replace("\\", "\\\\") + '"'
            lines.append(f"{key} = {rendered}\n")
    target.write_text("".join(lines), encoding="utf-8")
    return target


__all__ = [
    "Config",
    "ConfigError",
    "DEFAULT_CONFIG",
    "ENV_PREFIX",
    "EnviSection",
    "IdlSection",
    "LimitsSection",
    "PathsSection",
    "SECTION_TYPES",
    "SecuritySection",
    "ServerSection",
    "count_task_templates",
    "load_config",
    "project_root",
    "write_default_config",
]
