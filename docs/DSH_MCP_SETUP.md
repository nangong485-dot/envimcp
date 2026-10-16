# Wiring envimcp into DeepSeek Harness (DSH)

DSH is a Cordis-based plugin host. It does **not** read a `mcpServers` JSON blob:
every plugin — MCP servers included — is a *row* in the profile's composition,
and the user-editable half of that composition is a single patch file.

Nothing has to be downloaded: the MCP bridge plugin (`@deepseek-ai/dsh-mcp-client`)
ships inside DSH itself. You are only adding a row that points it at envimcp.

- [1. Locate the profile directory](#1-locate-the-profile-directory)
- [2. Make sure envimcp is healthy](#2-make-sure-envimcp-is-healthy)
- [3. Add the patch entry](#3-add-the-patch-entry)
- [4. `dsh-mcp-client` configuration keys](#4-dsh-mcp-client-configuration-keys)
- [5. What you get](#5-what-you-get)
- [6. Verify it took effect](#6-verify-it-took-effect)
- [7. Troubleshooting](#7-troubleshooting)
- [8. The same server in other clients](#8-the-same-server-in-other-clients)
- [9. Rollback](#9-rollback)

Throughout, `<repo>` is the checkout root of this project (the directory holding
`pyproject.toml`) and `<profile>` is the DSH profile name — `desktop` for a
standard desktop install. Substitute your own absolute paths; the examples below
intentionally use generic ones.

## 1. Locate the profile directory

```text
%USERPROFILE%\.dsh\profiles\<profile>\
    cordis.yml          <- composed root; do NOT edit (it is generated)
    cordis.patch.yml    <- the user patch layer; THIS is the file you edit
    package.json        <- dependencies and dsh.profile.bundles
    pnpm-lock.yaml
```

`cordis.yml` is an empty entry list on purpose. The live tree is composed by
applying, in order: every bundle listed in `package.json` →
`dsh.profile.bundles`, then `cordis.patch.yml`, then any `--patch` overlays. If
you edit `cordis.yml` your change is simply overwritten.

## 2. Make sure envimcp is healthy

An MCP host can only register a server that starts and speaks the protocol
cleanly, so get `doctor` to pass first:

```powershell
$repo = '<repo>'
$py   = "$repo\.venv\Scripts\python.exe"
$cfg  = "$repo\config\envimcp.toml"

# only if .venv does not exist yet
D:\path\to\python312\python.exe -m venv "$repo\.venv"
& $py -m pip install -r "$repo\requirements.txt"

& $py -m envimcp.cli --config $cfg doctor
```

`doctor` exits `0` with `verdict = ready` when the IDL executable, the ENVI
installation and the task templates all resolve. If it reports problems, edit
`config/envimcp.toml` (`idl_exe`, `envi_root`, `task_template_dir`,
`sample_raster`) until it is green — a misconfigured server will look like a DSH
bug later.

## 3. Add the patch entry

`cordis.patch.yml` is a top-level YAML array of patch entries. The rule that
trips everyone up:

- A **plain** entry (`- id: ... / name: ... / config: ...`) only *modifies* a row
  that already exists. It is matched by `id`; if no row has that id the entry is
  skipped (with a warning). You cannot add a new plugin this way.
- To **add** a plugin you use `insert:` with a row that deliberately has **no
  `id`**, which appends it to the composition.

So the entry you want, appended to the end of the file:

```yaml
# envimcp — ENVI 5.2 headless ENVITask operator bridge, launched as a local
# stdio MCP server. Tools appear as envi_* in the model's tool list.
- insert:
    - id: mcp-envi
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: envi
        transport: stdio
        command: 'C:\path\to\envimcp\.venv\Scripts\python.exe'
        args:
          - '-m'
          - 'envimcp.cli'
          - '--config'
          - 'C:\path\to\envimcp\config\envimcp.toml'
          - 'serve'
          - '--transport'
          - 'stdio'
        cwd: 'C:\path\to\envimcp'
        failOnStartupError: true
```

Details that matter:

- **Quote Windows paths with single quotes.** In YAML a double-quoted string
  treats `\` as an escape, so `"C:\path\to\x"` is `C:pathtox` plus a broken
  escape. Single quotes are literal.
- `serverName` is the identifier the host exposes: it names the resource server
  (`envi://...`), the `list_mcp_resources`/`read_mcp_resource` server argument,
  and the prefix of any tool the server would otherwise contribute. Keep it
  unique per registration scope.
- `id: mcp-envi` on the inserted row is arbitrary and only needs to be unique
  locally; it is the handle you would use to later patch or disable that row.
- Use absolute paths for `command` (a bare `python` is not resolvable by the
  host) and prefer an explicit `cwd`.

## 4. `dsh-mcp-client` configuration keys

| Key | Required | Default | Meaning |
|---|---|---|---|
| `transport` | yes | — | `stdio` for a local program, `streamable-http` for a remote service |
| `serverName` | yes | — | `[A-Za-z0-9_-]{1,32}`, unique per registration scope |
| `command` | stdio | — | Absolute path to the executable to launch |
| `args` | stdio | `[]` | Argument array, passed verbatim |
| `env` | stdio | `{}` | Merged *over* a scrubbed copy of the ambient environment |
| `cwd` | stdio | host default | Working directory for the child process |
| `url` | http | — | Endpoint, e.g. `http://127.0.0.1:8765/mcp` |
| `headers` | http | `{}` | Extra request headers |
| `toolCallTimeoutMs` | no | `60000` | Per-tool-call timeout |
| `maxInstructionBytes` | no | `32768` | Cap on the server instructions injected into the prompt |
| `failOnStartupError` | no | `false` | Reject plugin activation if the initial connect or tool sync fails |

For the HTTP transport, replace `command`/`args`/`cwd` with the URL and start the
server yourself as a long-lived process:

```yaml
        transport: streamable-http
        url: http://127.0.0.1:8765/mcp
        failOnStartupError: true
```

```powershell
& $py -m envimcp.cli --config $cfg serve --transport http --port 8765
```

One stdio server per host is the low-friction choice for a single-user desktop:
no port to manage, and the host owns the process lifetime.

## 5. What you get

The tool list is read from the live server at connect time, so it always matches
[README.md](README.md#tools) — eight tools
(`envi_list_tasks`, `envi_get_task_schema`, `envi_run_task`,
`envi_create_task_workflow`, `envi_inspect_raster`, `envi_export_raster`,
`envi_job_status`, `envi_job_cancel`) plus five resources
(`envi://config`, `envi://tasks`, `envi://task/{name}`, `envi://jobs`,
`envi://job/{job_id}`).

`envi_run_idl` is absent until you set `security.allow_raw_idl = true` in
`config/envimcp.toml`, on purpose: it runs arbitrary IDL.

The resource half is provided by the companion `@deepseek-ai/dsh-mcp-resources`
plugin, which the host activates automatically once at least one server is
configured. Without it you would get only the tools.

## 6. Verify it took effect

DSH re-reads the patch layer on reload — in testing, the new server was live
without restarting the app. Two independent checks, the second worth doing
whenever you change anything:

**a) The host's own view.** After the edit, the runtime context gains a
`### MCP server: envi` instruction block and an MCP resource-server list naming
`["envi"]`, and the `envi_*` tools appear alongside native tools. If the tools
table shows up but calls fail, the server started and then died — check the host
log and run `doctor`.

**b) A raw stdio handshake**, bypassing the host entirely. Build the server
command as an array so PowerShell passes each argument separately:

```powershell
$server = @($py, '-m', 'envimcp.cli', '--config', $cfg, 'serve', '--transport', 'stdio')
& $py "$repo\tools\probe_mcp_stdio.py" -- @server
```

Expected output: `serverInfo` = `envimcp`, the negotiated protocol version, the
tool count (`8`), the resource count (`3`) and the template count (`2`). This
proves the exact command line in your patch entry works as written, which is the
part most likely to be wrong.

### About the process count

On Windows a `venv` interpreter is a small launcher that re-executes the real
interpreter, so **one** envimcp server shows up as **two** `python.exe`
processes. Hosts may also keep one server instance per session or per client, so
seeing several pairs is normal — an idle server holds no IDL process and no
licence. What you should not see is a steady upward creep with no client
attached; that is a host-side reconnect loop, not an envimcp problem.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Server never appears, no error | The entry was written as a plain `- id: ...` row instead of `insert:`. A non-insert patch that matches no existing row is skipped, and the log line is easy to miss. |
| Host fails to start after editing | The patch file is parsed at boot and a bad file fails loudly. Validate with `& $py -c "import yaml;yaml.safe_load(open(r'C:\path\to\cordis.patch.yml',encoding='utf-8'))"` before restarting. |
| Plugin activation rejected, tools missing | Deliberate with `failOnStartupError: true`: the command line or the venv is wrong. Run check (b) above; its error is the real one. Set the flag to `false` to downgrade a broken server to silence — convenient, easy to forget. |
| `EINVAL`/immediate exit, empty tool list | `command` was not an absolute path, or `args` was written as one space-separated string instead of a list. |
| A stray `envimcp` server per start | Stdio negotiation launches a temporary probe process before the serving process; it should exit on its own. If pairs accumulate while no client runs jobs, restart the host. |
| Two hosts fight over one ENVI licence | `limits.max_concurrent = 1` serializes jobs *inside* one server, not across servers. The Codex/ChatGPT desktop app and DSH each get their own instance, so run jobs from one host at a time. |
| `The output URI already exists: !name` | Unrelated to DSH: a host token was passed through to ENVI literally. See [IDL_NOTES.md](IDL_NOTES.md). |

## 8. The same server in other clients

The command line is identical everywhere; only the config shape changes.

**Codex / ChatGPT desktop** — `%USERPROFILE%\.codex\config.toml`:

```toml
[mcp_servers.envi]
command = 'C:\path\to\envimcp\.venv\Scripts\python.exe'
args = ["-m", "envimcp.cli", "--config", 'C:\path\to\envimcp\config\envimcp.toml', "serve", "--transport", "stdio"]
```

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "envi": {
      "command": "C:\\path\\to\\envimcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "envimcp.cli", "--config", "C:\\path\\to\\envimcp\\config\\envimcp.toml",
               "serve", "--transport", "stdio"]
    }
  }
}
```

**Anything else** — [README.md](README.md#mcp-client-setup) has the generic
`mcpServers` form; the HTTP endpoint is the fallback for clients that cannot
spawn processes.

## 9. Rollback

Delete the `- insert:` block you added from `cordis.patch.yml` (and the
`[mcp_servers.envi]` / `mcpServers.envi` block from any other client) and restart
the host. Nothing else in the profile depends on it, and envimcp itself is
untouched — `doctor`, the CLI and the tests keep working on their own.
