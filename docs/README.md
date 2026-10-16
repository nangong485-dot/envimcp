# envimcp — drive ENVI 5.2 from an AI agent over MCP

**English** | [简体中文](README.zh-CN.md)

`envimcp` exposes the 58 ENVITask operators of a local **ENVI 5.2 / IDL 8.4**
installation to any MCP client (DeepSeek Harness, Codex, ZCode, Claude Desktop,
…) as eight tools and five resources, with no ENVI GUI and no Python bridge.

```
MCP client (agent)
   │  stdio  or  streamable HTTP
   ▼
envimcp MCP server  (Python 3.12, mcp 2.3.0)
   ├─ TaskCatalog   reads D:\ENVI52\resource\templates\tasks\*.task   (58 operators)
   ├─ ScriptGen     emits literal-property IDL source per job
   └─ JobRunner     spawns  idl.exe -e "@<job>\run_job.pro"  (1 job at a time)
   ▼
IDL 8.4 process → ENVI(/HEADLESS) → ENVITask → status.json
```

## Why not `envipyengine`

The obvious approach for "Python drives ENVI" does not work on this machine, for
two independent reasons:

1. **IDL 8.4 ships no IDL-Python bridge.** There is no `omniidl`, no `*.pyd` and
   no Python DLL anywhere under `D:\IDL84`; `D:\IDL84\lib\bridges` contains only
   Java and COM bridges. NV5's own documentation: *"IDL versions prior to 8.5 do
   not support Python bridge functionality."*
2. **Stronger: `envipyengine` does not use the bridge at all.** It spawns
   `taskengine.exe` and exchanges JSON on stdin/stdout, and that executable first
   appears in **IDL 8.5.2 / ENVI 5.3** (`idltask.html` version history:
   `8.5.2 | Introduced`). IDL 8.5 alone is still not enough.

So this project takes the route that *is* supported: ENVI 5.2's documented
`ENVI(/HEADLESS)`, driven through `idl.exe -e`. The price is that ENVI task
parameters can only be set as **compile-time literal keywords**, so Python
generates IDL source instead of making calls. See
[IDL_NOTES.md](IDL_NOTES.md) for the 24 traps that shaped the generator.

## Install

Everything below assumes a local ENVI 5.2 / IDL 8.4 install and one checkout of
this repository. `<repo>` stands for the absolute path of that checkout — it may
contain spaces, which is why every command below quotes it. The project has only
been verified on the machine that produced it. Create the environment first — the
`.venv` directory is **not** part of the source tree:

```powershell
# 1. create/reuse .venv, install the package, print a health report
pwsh -File '<repo>\tools\setup_env.ps1'
#    ...or, if the interpreter is not at the default path:
pwsh -File '<repo>\tools\setup_env.ps1' -Python D:\python312\python.exe
```

Then, from the repository root:

```powershell
$root = '<repo>'
& "$root\.venv\Scripts\python.exe" -m envimcp.cli --config "$root\config\envimcp.toml" doctor
```

`setup_env.ps1` needs `D:\python312\python.exe` (bare `python` does not resolve in
this shell) and reaches packages through the Tsinghua mirror, because `pypi.org`
is unreachable from this network. It finishes by running `doctor`, which prints a
JSON health report and exits non-zero if anything is missing.

## Configure

[`config/envimcp.toml`](../config/envimcp.toml) is the only configuration file.
Every key can be overridden by an `ENVIMCP_*` environment variable.

| Section | Key | Meaning |
|---|---|---|
| `envi` | `root` | ENVI installation (`D:\ENVI52`) |
| `envi` | `task_template_dir` | Where the `*.task` definitions live |
| `envi` | `sample_raster` | Dataset used by `smoke` (the bundled QuickBird demo) |
| `idl` | `exe` | **`D:\IDL84\bin\bin.x86_64\idl.exe`** — not `envi_idl.exe`/`idlde.exe` |
| `paths` | `jobs_dir`, `outputs_dir` | Where jobs and `#name` outputs land |
| `limits` | `max_concurrent`, `on_busy`, `timeout_s` | ENVI is single-licensed: one job at a time |
| `server` | `name`, `transport`, `host`, `port` | MCP identity and listen address |
| `security` | `allow_raw_idl` | Registers the `envi_run_idl` escape hatch (default **false**) |
| `security` | `allow_outputs_outside_root` | Allows writes outside the job/output roots (default **false**) |

`envi_idl.exe` and `idlde.exe` are byte-identical Eclipse Workbench GUI launchers
(MD5 `E786D30B00ACE364B26657F98D79F03A`) and are the wrong binary.

## Command line

```powershell
$py  = '<repo>\.venv\Scripts\python.exe'
$cfg = '<repo>\config\envimcp.toml'

& $py -m envimcp.cli --config $cfg doctor            # is this machine usable?
& $py -m envimcp.cli --config $cfg smoke             # end-to-end NDVI regression
& $py -m envimcp.cli --config $cfg tasks spectral    # search the operator catalog
& $py -m envimcp.cli --config $cfg schema SpectralIndex
& $py -m envimcp.cli --config $cfg inspect D:\ENVI52\data\qb_boulder_msi
& $py -m envimcp.cli --config $cfg run SpectralIndex `
      --input INPUT_RASTER=D:\ENVI52\data\qb_boulder_msi --input INDEX=NDVI --output '#ndvi.dat'
& $py -m envimcp.cli --config $cfg export D:\ENVI52\data\qb_boulder_msi '#msi.tif'
& $py -m envimcp.cli --config $cfg serve --transport stdio
& $py -m envimcp.cli --config $cfg serve --transport http --port 8765
```

`smoke` asserts the NDVI statistics against the measured golden baseline within
`1e-6` and prints the deltas:

| quantity | golden value |
|---|---|
| `min` | `-0.43410853` |
| `max` | `0.62695548` |
| `mean` | `0.10097130` |
| `stddev` | `0.14894113` |
| `finite_count` | `1048576` |

## MCP client setup

**stdio** (DeepSeek Harness, Codex, ZCode):

```json
{
  "mcpServers": {
    "envi": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": [
        "-m", "envimcp.cli",
        "--config", "<repo>\\config\\envimcp.toml",
        "serve", "--transport", "stdio"
      ]
    }
  }
}
```

**streamable HTTP** (long-lived server, multiple clients):

```powershell
& $py -m envimcp.cli --config $cfg serve --transport http --port 8765
```

The endpoint is `http://127.0.0.1:8765/mcp`.

DeepSeek Harness does not use a JSON blob: it registers servers by patching its
profile composition. The exact entry, the configuration keys it accepts, and a
dependency-free handshake probe for verifying *any* client are in
[DSH_MCP_SETUP.md](DSH_MCP_SETUP.md).

## Tools

| Tool | Purpose |
|---|---|
| `envi_list_tasks(query, limit)` | List/search operators by name, display name or description |
| `envi_get_task_schema(name)` | Full JSON Schema plus accepted `choiceList` values |
| `envi_run_task(task, inputs, output, timeout_s, wait)` | Run one operator; returns output geometry and statistics |
| `envi_create_task_workflow(steps, timeout_s, wait)` | Chain operators, wiring intermediate rasters without files |
| `envi_inspect_raster(path, stats, use_envi)` | Metadata/statistics; reads the file directly unless `use_envi` |
| `envi_export_raster(path, destination, format, wait)` | Write GeoTIFF or ENVI via `ENVIRaster::Export` |
| `envi_job_status(job_id)` | Status, result, outputs and an IDL output tail |
| `envi_job_cancel(job_id)` | Cancel and kill the job's IDL process tree |
| `envi_run_idl(source, timeout_s)` | Escape hatch; **only registered when `security.allow_raw_idl = true`** |

Every tool that starts work returns a `job_id` even when it also waits, because
ENVI is single-licensed: a second request either queues or is rejected according
to `limits.on_busy`.

## Resources

| URI | Contents |
|---|---|
| `envi://config` | Effective configuration plus the health checks `doctor` runs |
| `envi://tasks` | The whole catalog (58 tasks) — read this instead of 58 tool schemas |
| `envi://task/{name}` | One task's schema, choices and source `.task` path |
| `envi://jobs` | The most recent jobs, newest first |
| `envi://job/{job_id}` | One job record including every step result |

## Output URI tokens

| Form | Destination | Use for |
|---|---|---|
| `#name.dat` | `<outputs_dir>/name.dat` | Deliverables |
| `!name.dat` | `<job_dir>/name.dat` | Intermediates, throwaway results |
| `*name` | nowhere | A workflow intermediate passed as a runtime handle |
| absolute path | as given | Must resolve inside a permitted root |

A bare filename (`name.dat`) is rejected — an unconstrained path is an
unconstrained write. `.dat` is appended when the name has no known extension.

> **ENVI 5.2 caveat:** the `*name` token is *not* an ENVI feature. ENVI treats
> the URI literally, so `envimcp` gives the task a throwaway temporary file
> (`e.GetTemporaryFilename()`) and carries only the runtime raster handle to the
> next step. The observable contract is what matters: no intermediate file is
> delivered, and the next step sees the in-memory raster.

## Typical agent flow

1. `envi_list_tasks("vegetation")` — find candidate operators.
2. `envi_get_task_schema("SpectralIndex")` — read required parameters and, for
   `INDEX`, the 64 accepted names. Note `NDVI` is **not** one of them; `envimcp`
   resolves the acronym, but the long name is `Normalized Difference Vegetation
   Index`.
3. `envi_run_task(task="SpectralIndex", inputs={...}, output="#ndvi.dat")`.
4. Read `steps[0].stats` and `steps[0].raster` from the result.

For multi-step chains use `envi_create_task_workflow` with `*NAME` wiring:

```json
{
  "steps": [
    {"task": "SpectralIndex",
     "inputs": {"INPUT_RASTER": "D:/ENVI52/data/qb_boulder_msi", "INDEX": "NDVI"},
     "outputs": {"OUTPUT_RASTER": "*NDVI"}},
    {"task": "LinearPercentStretchRaster",
     "inputs": {"INPUT_RASTER": "*NDVI", "PERCENT": 2.0},
     "outputs": {"OUTPUT_RASTER": "!stretched.dat"}}
  ]
}
```

## Security model

* `envi_run_idl` is off by default. When on it executes arbitrary IDL with the
  full privileges of the ENVI session.
* Absolute output paths must resolve inside `outputs_dir`, `jobs_dir` or an
  explicitly pinned output root unless `allow_outputs_outside_root` is set.
* The `#`/`!`/`*` tokens accept only bare file names, so a token cannot escape
  its directory (`#../../evil` is rejected, not silently relocated).
* MCP resources use `ResourceSecurity(reject_path_traversal=True,
  reject_absolute_paths=True, reject_null_bytes=True)` — the mcp 2.x default.
* Every job is confined to `jobs/<id>/`; its `status.json` is the verdict.

## Testing

```powershell
cd '<repo>'
& .\.venv\Scripts\python.exe -m pytest tests -q -m "not slow"   # 52 fast tests, ~1.5 s
& .\.venv\Scripts\python.exe -m pytest tests -q -m slow         # 4 acceptance tests, ~20 s
```

The slow tests really launch `idl.exe` and re-assert the golden NDVI baseline
(AC1), the virtual-raster workflow (AC6), the inspect path and a real GeoTIFF.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Job ends with no `status.json`, `idl.log` empty, IDL exits 0 | The `-e "@<batch>"` file was never opened. The path must use **forward slashes**, unquoted. |
| `% Attempt to call undefined function: 'HASH'` | The batch file never ran, or an identifier exceeded IDL's 32-character limit. |
| `% Array dimensions must be greater than 0.` | A zero-length array reached a `HASH` assignment (`STRARR(0)` aborts IDL). |
| `The output URI already exists: !name` | A host token was handed to ENVI literally instead of being resolved. |
| `Input value is of type STRING but the parameter is expected to be of type ENVIRASTER` | Set `<PARAM>_URI` for paths, not `<PARAM>`. |
| `% JOBID is already defined with a conflicting definition.` | A routine parameter shares a name with a common-block variable. |
| `ENVI(/HEADLESS)` returns an invalid object | Licensing, or another ENVI/IDL process still holds the single licence. |
| Every later job fails after one bad job | An orphan `idl.exe` kept the licence; `JobManager` reaps it automatically. |

Deterministic detail for each of these lives in [IDL_NOTES.md](IDL_NOTES.md).
