# envimcp —— 通过 MCP 让 AI 智能体驱动 ENVI 5.2

[English](README.md) | **简体中文**

`envimcp` 把本机 **ENVI 5.2 / IDL 8.4** 安装里的 58 个 ENVITask 算子，以 **8 个工具 + 5 个资源**的形式暴露给任意 MCP 客户端（DeepSeek Harness、Codex、ZCode、Claude Desktop……），全程不开 ENVI 图形界面，也不需要 Python 桥。

```
MCP 客户端（智能体）
   │  stdio 或 streamable HTTP
   ▼
envimcp MCP 服务端   (Python 3.12, mcp 2.3.0)
   ├─ TaskCatalog   读取 D:\ENVI52\resource\templates\tasks\*.task   (58 个算子)
   ├─ ScriptGen     为每个作业生成「字面量属性」形式的 IDL 源码
   └─ JobRunner     启动  idl.exe -e "@<job>\run_job.pro"  (同一时刻只跑 1 个作业)
   ▼
IDL 8.4 进程 → ENVI(/HEADLESS) → ENVITask → status.json
```

## 为什么不用 `envipyengine`

"用 Python 驱动 ENVI"这条最直觉的路在这台机器上走不通，有两个彼此独立的理由：

1. **IDL 8.4 根本没有 IDL-Python 桥。** `D:\IDL84` 下没有 `omniidl`、没有任何 `*.pyd`、也没有 Python DLL；`D:\IDL84\lib\bridges` 里只有 Java 桥和 COM 桥。NV5 自己的文档写着：*"IDL versions prior to 8.5 do not support Python bridge functionality."*
2. **更要命的是，`envipyengine` 压根不用那个桥。** 它是启动 `taskengine.exe` 并在 stdin/stdout 上交换 JSON，而这个可执行文件最早出现在 **IDL 8.5.2 / ENVI 5.3**（`idltask.html` 的版本历史：`8.5.2 | Introduced`）。光有 IDL 8.5 仍然不够。

所以本项目走的是**真正被支持**的那条路：ENVI 5.2 有文档保证的 `ENVI(/HEADLESS)`，通过 `idl.exe -e` 驱动。代价是 ENVI 的算子参数**只能以编译期字面量关键字**的方式设置，于是 Python 改为生成 IDL 源码而不是发调用。塑造了这套生成器的 24 个坑见 [IDL_NOTES.md](IDL_NOTES.md)。

## 安装

以下内容假定：本机装有 ENVI 5.2 / IDL 8.4，且你已有一份本仓库的检出。`<repo>` 指这份检出目录的绝对路径——它**可能含空格**，所以下面每条命令都加了引号。本项目只在产出它的那台机器上验证过。先建环境，`.venv` 目录**不属于**源码树：

```powershell
# 1. 创建/复用 .venv，安装包，打印健康报告
pwsh -File '<repo>\tools\setup_env.ps1'
#    ……或者解释器不在默认位置时：
pwsh -File '<repo>\tools\setup_env.ps1' -Python D:\python312\python.exe
```

然后在仓库根目录下：

```powershell
$root = '<repo>'
& "$root\.venv\Scripts\python.exe" -m envimcp.cli --config "$root\config\envimcp.toml" doctor
```

`setup_env.ps1` 需要 `D:\python312\python.exe`（这个 shell 里裸写 `python` 解析不到），并通过清华镜像装包，因为本网络访问不了 `pypi.org`。脚本最后会跑一次 `doctor`：它打印 JSON 健康报告，缺东西就以非零码退出。

## 配置

[`config/envimcp.toml`](../config/envimcp.toml) 是唯一的配置文件。每个键都能用 `ENVIMCP_*` 环境变量覆盖。

| 段 | 键 | 含义 |
|---|---|---|
| `envi` | `root` | ENVI 安装目录（`D:\ENVI52`） |
| `envi` | `task_template_dir` | `*.task` 算子定义所在目录 |
| `envi` | `sample_raster` | `smoke` 使用的数据集（ENVI 随附的 QuickBird 样例） |
| `idl` | `exe` | **`D:\IDL84\bin\bin.x86_64\idl.exe`**——不是 `envi_idl.exe`/`idlde.exe` |
| `paths` | `jobs_dir`、`outputs_dir` | 作业目录与 `#name` 输出的落地位置 |
| `limits` | `max_concurrent`、`on_busy`、`timeout_s` | ENVI 是单许可的：同一时刻只能跑一个作业 |
| `server` | `name`、`transport`、`host`、`port` | MCP 身份与监听地址 |
| `security` | `allow_raw_idl` | 是否注册 `envi_run_idl` 这个逃生舱（默认 **false**） |
| `security` | `allow_outputs_outside_root` | 是否允许写到作业/输出根目录之外（默认 **false**） |

`envi_idl.exe` 与 `idlde.exe` 是**逐字节相同**的 Eclipse Workbench 图形启动器（MD5 `E786D30B00ACE364B26657F98D79F03A`），不是我们要的那个二进制。

## 命令行

```powershell
$py  = '<repo>\.venv\Scripts\python.exe'
$cfg = '<repo>\config\envimcp.toml'

& $py -m envimcp.cli --config $cfg doctor            # 这台机器能用吗？
& $py -m envimcp.cli --config $cfg smoke             # 端到端 NDVI 回归
& $py -m envimcp.cli --config $cfg tasks spectral    # 检索算子目录
& $py -m envimcp.cli --config $cfg schema SpectralIndex
& $py -m envimcp.cli --config $cfg inspect D:\ENVI52\data\qb_boulder_msi
& $py -m envimcp.cli --config $cfg run SpectralIndex `
      --input INPUT_RASTER=D:\ENVI52\data\qb_boulder_msi --input INDEX=NDVI --output '#ndvi.dat'
& $py -m envimcp.cli --config $cfg export D:\ENVI52\data\qb_boulder_msi '#msi.tif'
& $py -m envimcp.cli --config $cfg serve --transport stdio
& $py -m envimcp.cli --config $cfg serve --transport http --port 8765
```

`smoke` 会把 NDVI 统计量与实测基准在 `1e-6` 容差内比对，并打印偏差：

| 量 | 实测基准值 |
|---|---|
| `min` | `-0.43410853` |
| `max` | `0.62695548` |
| `mean` | `0.10097130` |
| `stddev` | `0.14894113` |
| `finite_count` | `1048576` |

## 接入 MCP 客户端

**stdio**（DeepSeek Harness、Codex、ZCode）：

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

**streamable HTTP**（常驻服务，供多个客户端共用）：

```powershell
& $py -m envimcp.cli --config $cfg serve --transport http --port 8765
```

端点即 `http://127.0.0.1:8765/mcp`。

DeepSeek Harness 不用 JSON 配置块：它靠给 profile 组合打补丁来注册服务器。要加的那一条、它接受的配置键，以及一个不依赖任何第三方库、可用来验证**任意**客户端的握手探针，都在 [DSH_MCP_SETUP.md](DSH_MCP_SETUP.md) 里。

## 工具

| 工具 | 用途 |
|---|---|
| `envi_list_tasks(query, limit)` | 按名称、显示名或描述列出/搜索算子 |
| `envi_get_task_schema(name)` | 完整 JSON Schema，外加可接受的 `choiceList` 取值 |
| `envi_run_task(task, inputs, output, timeout_s, wait)` | 运行一个算子；返回输出几何与统计量 |
| `envi_create_task_workflow(steps, timeout_s, wait)` | 串联算子，中间光栅以内存句柄传递、不落文件 |
| `envi_inspect_raster(path, stats, use_envi)` | 读元数据/统计量；默认直接读文件，除非指定 `use_envi` |
| `envi_export_raster(path, destination, format, wait)` | 通过 `ENVIRaster::Export` 写出 GeoTIFF 或 ENVI |
| `envi_job_status(job_id)` | 作业状态、结果、输出文件与 IDL 输出尾部 |
| `envi_job_cancel(job_id)` | 取消作业并杀掉它的 IDL 进程树 |
| `envi_run_idl(source, timeout_s)` | 逃生舱；**仅在 `security.allow_raw_idl = true` 时注册** |

任何会"开始干活"的工具都会返回 `job_id`，即使它同时等待完成——因为 ENVI 是单许可的：第二个请求要么排队，要么被拒，取决于 `limits.on_busy`。

## 资源

| URI | 内容 |
|---|---|
| `envi://config` | 生效配置，外加 `doctor` 跑的那些健康检查 |
| `envi://tasks` | 完整算子目录（58 个）——读它，而不是去读 58 份工具 schema |
| `envi://task/{name}` | 单个算子的 schema、可选取值与来源 `.task` 路径 |
| `envi://jobs` | 最近的作业，新的在前 |
| `envi://job/{job_id}` | 单个作业记录，含每一步的结果 |

## 输出 URI 令牌

| 写法 | 落地位置 | 用途 |
|---|---|---|
| `#name.dat` | `<outputs_dir>/name.dat` | 交付物 |
| `!name.dat` | `<job_dir>/name.dat` | 中间结果、用完即弃的结果 |
| `*name` | 不落盘 | 以运行时句柄形式传递的工作流中间结果 |
| 绝对路径 | 原样使用 | 必须落在某个被允许的根目录内 |

裸文件名（`name.dat`）会被拒绝——路径不受约束就等于写入不受约束。名字没有已知扩展名时会补上 `.dat`。

> **ENVI 5.2 的一个前提：** `*name` 令牌**不是** ENVI 的功能。ENVI 会把 URI 当字面量处理，所以 `envimcp` 给算子一个用完即弃的临时文件（`e.GetTemporaryFilename()`），只把运行时的光栅句柄带到下一步。真正要看的是可观测契约：不交付任何中间文件，而下一步拿到的是内存中的光栅。

## 智能体的典型流程

1. `envi_list_tasks("vegetation")`——找候选算子。
2. `envi_get_task_schema("SpectralIndex")`——读必填参数，以及 `INDEX` 可接受的 64 个名字。注意 `NDVI` **不在**其中；`envimcp` 会解析这个缩写，但它的长名是 `Normalized Difference Vegetation Index`。
3. `envi_run_task(task="SpectralIndex", inputs={...}, output="#ndvi.dat")`。
4. 从结果里读 `steps[0].stats` 与 `steps[0].raster`。

多步串联用 `envi_create_task_workflow`，配合 `*NAME` 接线：

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

## 安全模型

* `envi_run_idl` 默认关闭。打开后，它会以 ENVI 会话的全部权限执行任意 IDL。
* 绝对输出路径必须落在 `outputs_dir`、`jobs_dir` 或显式指定的输出根目录内，除非设置了 `allow_outputs_outside_root`。
* `#`/`!`/`*` 令牌只接受裸文件名，因此令牌无法越出它所在目录（`#../../evil` 会被拒绝，而不是被悄悄改写）。
* MCP 资源使用 `ResourceSecurity(reject_path_traversal=True, reject_absolute_paths=True, reject_null_bytes=True)`——即 mcp 2.x 的默认值。
* 每个作业都被限制在 `jobs/<id>/` 内；它的 `status.json` 就是最终结论。

## 测试

```powershell
cd '<repo>'
& .\.venv\Scripts\python.exe -m pytest tests -q -m "not slow"   # 52 个快速测试，约 1.5 秒
& .\.venv\Scripts\python.exe -m pytest tests -q -m slow         # 4 个验收测试，约 20 秒
```

慢测试会真的启动 `idl.exe`，重新断言 NDVI 基准（AC1）、虚拟光栅工作流（AC6）、inspect 路径以及一个真实的 GeoTIFF。

## 排错

| 现象 | 原因 |
|---|---|
| 作业结束但没有 `status.json`，`idl.log` 为空，IDL 退出码是 0 | `-e "@<batch>"` 那个文件从未被打开。路径必须用**正斜杠**，且不加引号。 |
| `% Attempt to call undefined function: 'HASH'` | 批处理文件没跑起来，或者某个标识符超过了 IDL 的 32 字符上限。 |
| `% Array dimensions must be greater than 0.` | 有零长数组走到了 `HASH` 赋值（`STRARR(0)` 会让 IDL 中断）。 |
| `The output URI already exists: !name` | 宿主令牌被原样交给了 ENVI，而不是先被解析。 |
| `Input value is of type STRING but the parameter is expected to be of type ENVIRASTER` | 传路径要用 `<PARAM>_URI`，而不是 `<PARAM>`。 |
| `% JOBID is already defined with a conflicting definition.` | 某个例程参数名与 common block 变量重名。 |
| `ENVI(/HEADLESS)` 返回非法对象 | 许可问题，或者另有 ENVI/IDL 进程仍占着这个单许可。 |
| 一个坏作业之后，后续作业全部失败 | 某个孤儿 `idl.exe` 占着许可；`JobManager` 会自动回收它。 |

以上每一条的确定性细节见 [IDL_NOTES.md](IDL_NOTES.md)。
