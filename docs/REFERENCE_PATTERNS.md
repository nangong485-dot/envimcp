# Reference patterns adopted from NV5's own ENVI tooling

The most useful prior art is NV5's official
[`interactive-data-language/vscode-idl`](https://github.com/interactive-data-language/vscode-idl)
extension, which ships an ENVI MCP server, ENVITask parameter JSON Schemas and an
official ENVI batch-processing agent prompt.

Its **implementation** cannot be reused here: it depends on IDL 9.2+ and a
persistent IDLMCP execution session, and this machine runs IDL 8.4 / ENVI 5.2.
Its **patterns** transfer cleanly and were adopted:

## 1. URI tokens instead of raw paths

| token | meaning | adopted as |
|---|---|---|
| `#name` | persistent, in the outputs directory | `config.output_root_path` |
| `!name` | temporary, inside the job directory | `config.jobs_path/<job>/` |
| `*name` | virtual — never written | runtime handle registry |
| `**` | "all pixels" input reference | rejected as an output |

Rationale: an LLM client should not be inventing filesystem paths, and a
relative path is an unconstrained write. `src/envimcp/uris.py` implements the
tokens and rejects anything else.

The `*name` semantics differ in one honest way: NV5's stack can keep a raster
truly virtual, while **ENVI 5.2 cannot**. `envimcp` gives the task a throwaway
temporary file and carries only the handle, so the client-visible contract (no
intermediate file, next step sees the raster) is preserved without claiming a
capability the underlying version lacks.

## 2. Discover operators lazily, not one tool per task

Registering a tool per ENVITask would put 58 schemas into every client's context.
Instead:

* `envi_list_tasks` — compact name/description search
* `envi_get_task_schema` — the full schema on demand
* `envi://tasks` and `envi://task/{name}` — the same information for clients that
  browse resources rather than call tools

This is the same split NV5 uses (catalog listing, then per-task schema), and it is
why `envimcp` has eight tools rather than fifty-eight.

## 3. A fixed batch-processing contract for multi-file work

NV5's `enviBatchProcessing.prompt.md` prescribes the workflow shape for batch
agent work:

1. Process a **sample of 2–3 images first** to validate parameters.
2. Emit an explicit **progress checklist** so the run is resumable and auditable.
3. **Retry a failed file at most twice**, then move on and report it.

`envimcp` encodes the durable parts of this rather than the prompt text:

* one job = one `jobs/<id>/` directory containing `bootstrap.json`, the generated
  IDL, `status.json` and the outputs, so a chain is reproducible and inspectable;
* `limits.max_retries` (default 2) in the configuration;
* `envi_create_task_workflow` for the chain, and `envi_job_status` for progress.

The sampling/checklist discipline is left to the agent, since it is prompt-level
behaviour rather than a server capability.

## 4. Naming

Tools are `envi_*`, resources are `envi://*`, and each tool description states the
ENVI parameter name it maps to. A model that has seen ENVI documentation can then
guess correctly, and `envi_get_task_schema` confirms.

## 5. Task definitions, not hard-coded operators

Both implementations read `*.task` JSON rather than a hand-maintained operator
list, so a different ENVI installation exposes a different catalog with no code
change. `envimcp.catalog.load_catalog` parses the files directly, and
`catalog.load_failures` records anything it could not parse instead of failing
the whole server.
