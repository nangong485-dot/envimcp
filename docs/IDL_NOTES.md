# IDL 8.4 / ENVI 5.2 notes

Every statement below was reproduced against the real installation
(`D:\IDL84\bin\bin.x86_64\idl.exe` + `D:\ENVI52`) while building this project.
None of it is inferred from documentation. Each item is either encoded in the
generator/runner or pinned by a test in `tests/test_scriptgen.py`.

Read this before changing `src/envimcp/scriptgen.py`, `idl/envi_dsh_runner.pro`
or `idl/envi_dsh_util.pro`: most of it is the reason the code looks the way it
does.

## 1. How to start a headless ENVI

```idl
RESTORE, 'D:\IDL84\lib\hook\envi.sav'   ; mandatory: -e never auto-compiles ENVI
e = ENVI(/HEADLESS)                     ; a FUNCTION returning the application ref
; ... work ...
e.Close                                 ; a PROCEDURE; must precede EXIT
EXIT, STATUS = 0
```

* `ENVI(/HEADLESS)` yields `e.VERSION = '5.2'`, `e.TASK_NAMES` with 58 entries and
  `WIDGET_ID = 0`. It dates to ENVI 5 and is not a 5.3+ feature.
* Without the `RESTORE`, the call fails with
  `% Attempt to call undefined function: 'ENVI'`.
* `void = e.Close` fails with `% ENVI: Unknown property: CLOSE` — `Close` is a
  method invoked as a statement.
* If `e.Close` is omitted, `-e` hangs waiting on widgets.
* `envi.run` is 28 bytes of IDL (`e=envi(/SPLASH, /EXIT_IDL)`) — a GUI launcher,
  not a headless helper.

## 2. `idl.exe -e "@file"` semantics

* The file is executed as `$MAIN$` in **single-statement mode**: every line is a
  separate statement. A multi-line block construct is a syntax error there. The
  working pattern is a batch file that only does `.compile` plus one procedure
  call, with all logic inside compiled procedures.
* A trailing bare `END` in the batch file is a syntax error.
* Only the last `-e` is honoured; use `-e "@<absolute path>"`.
* IDL's working directory is **not** the shell's (it started in the user's home
  directory), so every path must be absolute.
* `.compile 'path'` — **no comma**. `.compile, 'path'` gives
  `% No such executive command: .compile,`.
* `COMPILE_OPT IDL2` at the main level of an `-e` file is rejected.
* **Argument form matters** (measured, same workspace):

  | argument | result |
  |---|---|
  | `@E:\...\run_job.pro` (backslash, bare) | exit 0, **0 modules compiled, silent no-op** |
  | `@"E:\...\run_job.pro"` | exit 0, `% Error opening file` (quotes became part of the name) |
  | `@E:/.../run_job.pro` (forward slash, bare) | exit 3, 37 modules compiled, **works** |
  | `@"E:/.../run_job.pro"` | exit 0, nothing happens |

  A path containing spaces (`E:\aiworkspace\deep seekharness\...`) is truncated at
  the space in the failing forms, which is why the failure looks like a missing
  file rather than a quoting bug. `Runner.command` therefore emits forward slashes.

* **Forcing `IDL_PATH` breaks everything.** Setting `IDL_PATH=+<IDL_DEFAULT>` (and
  `IDL_STARTUP`) makes IDL print its banner and exit immediately without running
  the batch file. `Runner.environment` returns `dict(os.environ)` unmodified.

* A PowerShell-written `.pro` with a UTF-8 **BOM** breaks `.compile`
  (`% Attempt to call undefined procedure: 'O'`). Generated files are written by
  Python's `Path.write_text`, which is BOM-free.

## 3. The exit code is not the verdict

IDL exits **0** after a silent no-op and **3** after a successful job whose step
set `failed`. `status.json` written by the driver is the only trustworthy result.

## 4. Identifiers are truncated at 32 characters

A generated routine named after a whole job id (e.g.
`job_20261012_210609_be83bb_STEP0`, 34 chars) compiles under a *different* name
than the call site resolves. The symptom is a misleading
`% Attempt to call undefined function: 'HASH'` and a run that dies before writing
anything.

Routine and file names therefore come from `_short_name()`:
`ENVI_DSH_<sha1(job_id|stem)[:8] upper>_<STEM>` (≤ 32 chars), and generated `.pro`
files are named after the **routine** (`plan.step_proc_path(i)`,
`plan.main_proc_path`). `tests/test_scriptgen.py` asserts both the length and the
file-name/routine-name equality.

## 5. ENVITask parameters are compile-time only

| attempt | result |
|---|---|
| `task.INPUT_RASTER = r` | works |
| `task.SetProperty, 'INPUT_RASTER', r` | `_ENVITASK::SETPROPERTY: Incorrect number of arguments.` |
| `task.SetProperty, _EXTRA=h` | `Invalid value for _EXTRA keyword: H.` |
| `task.GetProperty('NROWS')` | `Attempt to call undefined method: '...TASK::GETPROPERTY'` |
| `raster.GetProperty('NROWS')` | `ENVIRASTER::GETPROPERTY` undefined |

Consequence: Python must **generate literal IDL**, one module per step. There is
no data-driven alternative on IDL 8.4.

### Outputs use the `_URI` companion property

```idl
t.OUTPUT_RASTER_URI = '<absolute path>'   ; correct
t.OUTPUT_RASTER     = '<absolute path>'   ; Input value is of type STRING but the
                                          ; parameter is expected to be of type ENVIRASTER
```

The `name` in the `.task` JSON is the *parameter* name; the path goes to the
`<NAME>_URI` property. Read results back with `t.<NAME>` after `Execute`.

### Never hand ENVI a host token

`'!out.dat'` is treated as a literal filename:
`The output URI already exists: !out.dat`, and `t.OUTPUT_RASTER.URI` comes back as
the literal string while nothing is written. `'*NDVI'` is worse — the task reports
`the task produced no OUTPUT_RASTER`. Both tokens must be resolved in Python (or,
for `*`, replaced by a temporary file).

## 6. Language gaps in IDL 8.4

* **Hash literals do not parse**: `{ 'k': 1 }` → `% Syntax error`. Nested literal
  indexing `a[0]['key']` also fails. Struct literals `{task:'X', index:0}` do
  work, with dot access.
* `h = HASH()` + `h['k'] = v` works, and `l = LIST()` + `l.Add, x` + `x = l[0]`
  works (bind to a scalar before indexing).
* **`HASH()` is not resolvable from a module compiled by `.compile` inside
  `-e` batch mode.** Generated modules therefore use parallel key/value arrays and
  `ENVI_DSH_JOBJ`, never `HASH`.
* **An IDL array constructor flattens nested arrays.** `[ENVI_DSH_RAW(x)]` reaches
  the consumer as a *scalar*, so the "already-rendered JSON" marker is lost and
  every nested fragment comes back quoted (`"ok":"true"`). The container must be a
  `LIST` with one `.Add` per element.
* **Zero-length arrays are illegal**: `STRARR(0)` (and storing one in a `HASH`)
  aborts with `% Array dimensions must be greater than 0.` — and it aborts the
  *whole call stack* with no status file. Guard every "maybe empty" array.
* `IF failed THEN BEGIN ... ENDIF ELSE BEGIN ... ENDELSE` is a syntax error;
  legal forms are `IF ... THEN BEGIN ... ENDIF` alone, or
  `IF ... THEN ... ELSE ...` on one line. Seed defaults, then apply one override.
* `STRING()` returns `''` for BYTE values, so `STRING(OBJ_VALID(x))` prints empty
  (`STRING(FIX(OBJ_VALID(x)))` or a branch is needed).
* Procedures cannot `RETURN, value`; use a setter procedure plus a sentinel
  function (`ENVI_DSH_SETFAIL, msg, ENVI_DSH_FAILSTEP(...)`).
* `!ERROR_STATE` is cleared by `CATCH, /CANCEL`, so read
  `em = !ERROR_STATE.MSG` **before** cancelling.
* A function's return value cannot be used as a statement-level call.

## 7. A failed `.compile` in a batch file does not abort the batch

The batch keeps going, the driver procedure is still called, and IDL then exits
**silently**: no `status.json`, no `idl.log`, no diagnostics. That signature means
"a module was called but one of its routines failed to compile".

The minimal isolator is a batch containing only `.compile '<one .pro>'`; the raw
output prints the error with a caret at the offending token.

The most common cause is a **parameter name colliding with a common-block
variable**:

```
% JOBID is already defined with a conflicting definition.
```

`ENVI_DSH_JSTEPS` had a parameter named `jobId` while the same procedure's
`COMMON ENVI_DSH_RUN_COMMON` also declares `jobId`; the parameter was renamed to
`jobToken`.

## 8. Wrapping inside procedures

A probe written as a main-level batch file can silently skip whole `CATCH`/`IF`
blocks. Write probes as `PRO`s and call them. When something fails with no
message, walk the suspect routine statement by statement, one `CATCH` per step,
printing `!ERROR_STATE.MSG` and `!ERROR_STATE.SCOPE_NAME` to stdout.

Note also: a wrong theory is easy here. An `Attempt to call undefined function`
raised *inside* a `.compile`d procedure was reported by an outer catch block as
`cannot open raster <path>: Array dimensions must be greater than 0.` — the
prefix was the wrapper's text, and the real error had nothing to do with the
raster.

## 9. ENVI object model facts

* `ENVIRaster` has **no** `MIN`/`MAX`/`MEAN` properties
  (`ENVIRASTER: Unknown property: MIN`) — use `GetData()` and reduce it.
* `raster.DATA_TYPE` answers a **name string** (`'uint'`), not a numeric code.
  ENVI's numeric codes are also *not* IDL's `SIZE()` codes; see
  `ENVI_DATA_TYPES` in `src/envimcp/raster.py` and `ENVI_DSH_TYPE_CODE` in
  `idl/envi_dsh_util.pro`.
* `ENVI::ExportRaster` is deprecated; use `ENVIRaster::Export`, which accepts only
  `'envi'`, `'tiff'` and `'nitf'`. **ENVI 5.2 has no Cloud-Optimized GeoTIFF
  writer**, so a `cog` request is refused rather than silently downgraded;
  `raster.export_cog` reports `missing_dependency` unless GDAL/Rasterio is present.
* `e.OpenRaster('<basename>')` accepts the ENVI basename (no extension) when a
  sibling `.hdr` exists.
* Opening a raster, `GetData()`, `MIN`/`MAX`/`MEAN`/`WHERE(FINITE(...))` all work
  headlessly on the bundled QuickBird demo.

## 10. The `.task` catalog

* `D:\ENVI52\resource\templates\tasks\*.task` — exactly **58** files, every one
  `"invocationType": "keywords"`. No file name differs from its declared `name`.
* Each parameter carries **both** `name` (the documented property, e.g.
  `INPUT_RASTER`) and `keyword` (the legacy routine keyword, e.g. `TARGET`). They
  differ for roughly 180 parameters, and there are no keyword collisions within a
  task. Generated code assigns by `name`.
* `parameterType` is `required` (175) or `optional` (125).
* `choiceList` has **two shapes**: a JSON array of names
  (`SpectralIndex.INDEX`) or a JSON object mapping name → integer
  (`RadiometricCalibration.CALIBRATION_TYPE` = `{"Radiance": 0,
  "Top-of-Atmosphere Reflectance": 1, "Brightness Temperature": 2}`).
* `SpectralIndex.INDEX` takes 65 long English names (64 exposed here), e.g.
  `'Normalized Difference Vegetation Index'` — **not** `'NDVI'`. `envimcp`
  resolves unique acronyms, but the long name is what ENVI wants.
* **FLAASH is not an ENVITask in ENVI 5.2.** A recursive search for `*flaash*`
  `.task` files returns 0; FLAASH exists only as the Classic subsystem under
  `D:\ENVI52\classic\bin\flaash`. Atmospheric correction therefore means `QUAC`.

## 11. Licensing

No dongle or licence server was needed for any run, but IDL's banner prints
`Installation number: .` and `Licensed for use by: ` **empty**, so the licensing
mechanism is not fully understood. ENVI is single-licensed and process-global:
one job at a time, and an orphan `idl.exe` from a crashed job will fail every
later job until it is killed. `JobManager` reaps such orphans
(`Runner.terminate_orphans`, which only targets processes whose command line
contains that job's directory, so a hand-started ENVI session is never touched).

## 12. JSON encoding

* A value is "already-rendered JSON" **iff it is an array** — that is the type
  signal `ENVI_DSH_RAW` establishes. Do not sniff a string's *content* to decide
  whether it is JSON: doing so emitted `"uri":E:/.../out.dat` and `"file_type":,`
  because plain strings bypassed quoting and escaping.
* An empty hash must serialize to `{}` — see the `STRARR(0)` trap above.

## 13. Reference implementation

NV5's own `interactive-data-language/vscode-idl` extension contains a complete
ENVI MCP server and the official ENVI batch-processing agent contract
(`resources/agents/prompts/ENVI/enviBatchProcessing.prompt.md`: sample 2–3 images
first, emit a progress checklist, retry a failed file at most twice). Its
transport depends on IDL 9.2+'s `IDLMCPExecutionManager` and a persistent session,
so the *implementation* is unusable here — but its **patterns** (URI tokens,
three-tier task loading, `envi_*` naming) were adopted. See
[REFERENCE_PATTERNS.md](REFERENCE_PATTERNS.md).
