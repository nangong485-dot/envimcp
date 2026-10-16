; envi_dsh_runner.pro
;
; Headless ENVITask executor for envimcp.
;
; WHY THIS IS GENERATED-CODE-DRIVEN
; ---------------------------------
; IDL 8.4 gives us no way to set an ENVITask parameter whose name is only known
; at runtime. All three dynamic routes were probed against the real install and
; all fail:
;
;   task.INPUT_RASTER = r            ; keyword must be a compile-time token
;   task.SetProperty, 'INPUT_RASTER', r
;     -> _ENVITASK::SETPROPERTY: Incorrect number of arguments.
;   task.SetProperty, _EXTRA=h
;     -> Invalid value for _EXTRA keyword: H.
;
; The working form is a compile-time PROPERTY ASSIGNMENT, i.e. literal code.
; So Python (which owns the .task catalog and the type coercion) generates a
; procedure per step -- ``idl/gen/<job>_step0.pro`` -- whose body contains the
; literal assignments, plus a driver ``job_<id>.pro`` that calls them in order.
; This library only holds what stays generic: filesystem/JSON plumbing, raster
; description, statistics, and runtime-created raster handles for ``*`` wiring.
;
; IDL 8.4 ALSO CANNOT DO THIS                      (probed, syntax error)
; -------------------------------------------------
;   list[0]['key']         ; nested literal indexing is not parseable
;   h = { 'k': 1, 'j': 2 } ; hash literals are not syntactically valid
;   a = [{ 'k': 1 }]       ; "                             "
; HASH() + h['k'] = v and LIST() + a.Add, x are the working forms.
;
; CONTRACT
; --------
; Generated driver sets ENVI_DSH_OP / ENVI_DSH_JOBID / ENVI_DSH_STATUSFILE /
; ENVI_DSH_LOGFILE / ENVI_DSH_PROGRESSFILE in the ENVI_DSH_RUN_COMMON block and
; calls the library routines here. A generated step procedure returns 1 on
; success and 0 after calling ENVI_DSH_FAILSTEP. The driver writes the final
; ``status.json``; the Python host treats that file -- never the exit code --
; as the verdict, because IDL masks a failed batch with a generic code.
;
; Part of envimcp. Verified against IDL 8.4 + ENVI 5.2 on Windows.

;----------------------------------------------------------------------------
; Copy one file to a new location (used for '!' temporary outputs).
;
; The common block is declared IDENTICALLY in every routine here and in every
; generated module. That repeated declaration is what lets the driver, the
; generated steps and this library share one ENVI session and one
; virtual-raster registry.
;----------------------------------------------------------------------------
PRO ENVI_DSH_COPY, src, dst, OK = ok, ERRMSG = errmsg
  COMPILE_OPT IDL2
  ok = 0
  errmsg = ''
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    errmsg = !ERROR_STATE.MSG
    RETURN
  ENDIF
  d = STRING(dst)
  parent = FILE_DIRNAME(d)
  IF parent NE '' AND parent NE '.' THEN FILE_MKDIR, parent
  FILE_COPY, src, d, /OVERWRITE
  ok = 1
  CATCH, /CANCEL
END

;----------------------------------------------------------------------------
; Create (once) the ENVI session of this job. REASON is set for the host when
; the session cannot start -- that is almost always a licensing problem.
;----------------------------------------------------------------------------
PRO ENVI_DSH_START, OK = ok, ERRMSG = errmsg
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  ok = 0
  errmsg = ''
  IF OBJ_VALID(e) THEN BEGIN
    ok = 1
    RETURN
  ENDIF
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    errmsg = 'RESTORE of envi.sav failed: ' + !ERROR_STATE.MSG
    RETURN
  ENDIF
  ; A batch file launched with `idl.exe -e "@file"` runs in single-statement
  ; mode, where IDL does not auto-compile anything: ENVI lives in a SAVE file
  ; and is not resolvable by name. Calls failed with
  ; "% Attempt to call undefined function: 'ENVI'" until this RESTORE was added.
  sav = 'D:\IDL84\lib\hook\envi.sav'
  IF ~FILE_TEST(sav) THEN sav = 'D:\ENVI52\save\envi_framework.sav'
  IF ~FILE_TEST(sav) THEN BEGIN
    CATCH, /CANCEL
    errmsg = 'no ENVI SAVE file found; looked for ' $
             + 'D:\IDL84\lib\hook\envi.sav and D:\ENVI52\save\envi_framework.sav'
    RETURN
  ENDIF
  RESTORE, sav
  CATCH, /CANCEL
  IF ~ISA(e, 'ENVI') THEN BEGIN
    e = ENVI(/HEADLESS)
  ENDIF
  IF ~OBJ_VALID(e) THEN BEGIN
    CATCH, /CANCEL
    errmsg = 'ENVI(/HEADLESS) returned an invalid application object. This is almost always ' $
             + 'a licensing problem: check the ENVI license, and whether another ENVI/IDL ' $
             + 'process still holds it.'
    RETURN
  ENDIF
  appStarted = 1B
  enviVersion = STRING(e.VERSION)
  IF ~ISA(enviVersion, 'STRING') THEN enviVersion = 'unknown'
  ; A freshly started application has no ENVI tasks registered; ENVI registers
  ; them lazily, so touching TASK_NAMES here absorbs that one-time cost instead
  ; of having it land inside the first step's CATCH block.
  CATCH, probeErr
  IF probeErr NE 0 THEN BEGIN
    CATCH, /CANCEL
  ENDIF ELSE BEGIN
    dummy = e.TASK_NAMES
    CATCH, /CANCEL
  ENDELSE
  IF logFile NE '' THEN BEGIN
    IF FILE_TEST(logFile, /WRITE) THEN BEGIN
      CATCH, logErr
      IF logErr NE 0 THEN BEGIN
        CATCH, /CANCEL
      ENDIF ELSE BEGIN
        e.LOG_FILE = logFile
        e.LogMessage, 'envimcp job ' + jobId + ' starting, ENVI ' + enviVersion
        CATCH, /CANCEL
      ENDELSE
    ENDIF
  ENDIF
  ok = 1
END

;----------------------------------------------------------------------------
; Why failure handling is split in two:
;
; IDL will not let a procedure ``RETURN`` a value, and an IDL expression is
; evaluated before its caller runs -- which is exactly what clears
; ``!ERROR_STATE`` when the caller does ``CATCH, /CANCEL``. So the generated step
; captures ``em = !ERROR_STATE.MSG`` *first*, passes that string to
; ``ENVI_DSH_FAILSTEP`` (which memoizes it, and answers 0 so ``RETURN``-less
; flow can use it), and then calls ``ENVI_DSH_SETFAIL`` to hand the message
; back through the step's own ``msg`` output parameter.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_FAILSTEP, msg
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  failed = 1B
  failure = STRING(msg)
  RETURN, 0
END

PRO ENVI_DSH_SETFAIL, msg, ignored
  COMPILE_OPT IDL2
  msg = STRING(!ERROR_STATE.MSG)
  IF N_ELEMENTS(ignored) NE 0 THEN BEGIN
    IF STRING(ignored) NE '' THEN msg = STRING(ignored)
  ENDIF
END

;----------------------------------------------------------------------------
; Register a raster handle under NAME so a later step can use it through the
; ``*NAME`` token -- this is how virtual rasters pass between steps without ever
; touching the disk.
;----------------------------------------------------------------------------
PRO ENVI_DSH_REGISTER, raster, name
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  IF ~OBJ_VALID(raster) THEN RETURN
  IF rasterCount GE N_ELEMENTS(rasters) THEN BEGIN
    rasters = [rasters, raster]
    rasterNames = [rasterNames, STRUPCASE(STRING(name))]
    rasterCount += 1
  ENDIF ELSE BEGIN
    rasters[rasterCount] = raster
    rasterNames[rasterCount] = STRUPCASE(STRING(name))
    rasterCount += 1
  ENDELSE
END

;----------------------------------------------------------------------------
; Mark a value as already-rendered JSON so ENVI_DSH_JHASH emits it verbatim.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_RAW, text
  COMPILE_OPT IDL2
  RETURN, [STRING(text)]
END

;----------------------------------------------------------------------------
; Describe one output handle and stash it in the common block so it is not
; garbage-collected before the JSON is rendered. Returns the nested JSON text.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_DESCRIBE, output, label
  COMPILE_OPT IDL2
  IF ~OBJ_VALID(output) THEN RETURN, 'null'
  desc = ENVI_DSH_RDESC(output, STRING(label))
  ENVI_DSH_REGISTER, output, label
  RETURN, ENVI_DSH_JHASH(desc)
END

;----------------------------------------------------------------------------
; Retrieve a virtual (never-written) raster produced by an earlier step.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_VIRTUAL, label
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  u = STRUPCASE(STRING(label))
  FOR i = 0, rasterCount - 1 DO IF rasterNames[i] EQ u THEN RETURN, rasters[i]
  RETURN, !NULL
END

;----------------------------------------------------------------------------
; Execute the task that a generated step procedure just configured.
;
; Assumes the caller has already set the parameters and OUTPUT_RASTER_URI.
; Writes no JSON; the caller collects that after the raster is valid.
;----------------------------------------------------------------------------
PRO ENVI_DSH_EXEC, task, OK = ok, ERRMSG = errmsg
  COMPILE_OPT IDL2
  ok = 0
  errmsg = ''
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    errmsg = !ERROR_STATE.MSG
    RETURN
  ENDIF
  valid = task.Validate()
  IF ~valid THEN BEGIN
    CATCH, /CANCEL
    errmsg = 'ENVITask.Validate() reported invalid parameters'
    RETURN
  ENDIF
  task.Execute
  CATCH, /CANCEL
  ok = 1
END

;----------------------------------------------------------------------------
; Emit one per-step JSON document: idl/gen/<job>_step<index>.json.
;----------------------------------------------------------------------------
PRO ENVI_DSH_WRITESTEPJSON, rec, stepIndex
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  genDir = FILE_DIRNAME(statusFile) + '\gen'
  IF ~FILE_TEST(genDir, /DIRECTORY) THEN FILE_MKDIR, genDir
  path = genDir + '\' + jobId + '_step' + STRTRIM(LONG(stepIndex), 2) + '.json'
  ; ``rec`` is an already-rendered JSON object string produced by
  ; ENVI_DSH_JOBJ. An earlier revision passed a HASH here and rendered it with
  ; ENVI_DSH_JHASH; that silently degraded to '{}' once the generated modules
  ; stopped using HASH, which is why the step record was empty.
  text = STRING(rec)
  ENVI_DSH_WRITEFILE, path, text, OK = wok
  ENVI_DSH_PUSH, progressFile, text
  RETURN
END

;----------------------------------------------------------------------------
; Report an application-level failure discovered AFTER the first step ran, or
; by the driver when there were no steps at all.
;----------------------------------------------------------------------------
PRO ENVI_DSH_FAILRUN, reason, stepIndex, taskName, stepArg
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  failed = 1B
  failure = STRING(reason)
  ENVI_DSH_WRITESTEPJSON, ENVI_DSH_FAILREC(stepIndex, taskName, stepArg), stepIndex
END

;----------------------------------------------------------------------------
; Log one line to the IDL-side log and the progress journal.
;----------------------------------------------------------------------------
PRO ENVI_DSH_TRACE, text
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  ENVI_DSH_LOG, logFile, STRING(text)
END

;----------------------------------------------------------------------------
; Break long absolute paths so a compiler warning never truncates one.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_PATHSLICE, path, firstWidth, width
  COMPILE_OPT IDL2
  s = STRING(path)
  n = STRLEN(s)
  IF n LE firstWidth THEN RETURN, "  '" + s + "', $"
  first = STRMID(s, 0, firstWidth)
  pieces = "'" + first + "'"
  pos = firstWidth
  WHILE pos LT n DO BEGIN
    chunk = STRMID(s, pos, width)
    pieces = pieces + " + '" + chunk + "'"
    pos += width
  ENDWHILE
  RETURN, '  ' + pieces + ', $'
END

;----------------------------------------------------------------------------
; Open one raster and render its description as JSON into ``inspectJson``.
;
; Not an ENVITask: this is the cheap "look before you leap" call the batch
; workflow asks for first (sample a couple of datasets, check band count and
; statistics, and only then plan the chain).
;
; WANTSTATS / WANTHISTOGRAM are 1B/0B scalars -- the driver can only pass
; literal arguments, so the options are plain flags rather than a structure.
;----------------------------------------------------------------------------
PRO ENVI_DSH_INSPECT, rasterPath, wantStats, wantHistogram
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  ENVI_DSH_TRACE, 'inspect: ' + STRING(rasterPath)
  IF ~OBJ_VALID(e) THEN BEGIN
    failed = 1B
    failure = 'ENVI is not running, so no raster can be opened'
    RETURN
  ENDIF
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    em = !ERROR_STATE.MSG
    CATCH, /CANCEL
    failed = 1B
    failure = 'cannot open raster ' + STRING(rasterPath) + ': ' + STRING(em)
    RETURN
  ENDIF
  r = e.OpenRaster(STRING(rasterPath))
  IF ~OBJ_VALID(r) THEN BEGIN
    CATCH, /CANCEL
    failed = 1B
    failure = 'ENVI returned no raster for ' + STRING(rasterPath)
    RETURN
  ENDIF
  ENVI_DSH_REGISTER, r, 'INSPECT'
  ENVI_DSH_TRACE, 'inspect: raster open, nobj=' + STRING(OBJ_VALID(r))
  CATCH, descErr
  IF descErr NE 0 THEN BEGIN
    em = !ERROR_STATE.MSG
    CATCH, /CANCEL
    failed = 1B
    failure = 'describing ' + STRING(rasterPath) + ' failed: ' + STRING(em)
    RETURN
  ENDIF
  desc = ENVI_DSH_RDESC(r, '')
  CATCH, /CANCEL
  ENVI_DSH_TRACE, 'inspect: described, keys=' + STRING(N_ELEMENTS(desc.Keys()))
  stats = ENVI_DSH_RSTATS(r)
  ENVI_DSH_TRACE, 'inspect: stats keys=' + STRING(N_ELEMENTS(stats.Keys()))
  hist = HASH()
  IF wantHistogram THEN BEGIN
    data = r.GetData()
    IF N_ELEMENTS(data) GT 0 THEN BEGIN
      hbins = 256L
      finite = data[WHERE(FINITE(data))]
      IF N_ELEMENTS(finite) GT 0 THEN BEGIN
        mn = MIN(finite)
        mx = MAX(finite)
        hist = HASH()
        IF mx GT mn THEN BEGIN
          h = HISTOGRAM(finite, MIN=mn, MAX=mx, NBINS=hbins)
          hist['min'] = DOUBLE(mn)
          hist['max'] = DOUBLE(mx)
          hist['bins'] = LONG(hbins)
          hist['bin_width'] = DOUBLE((mx - mn) / hbins)
          IF N_ELEMENTS(h) GT 0 THEN hist['counts'] = LONG(h)
        ENDIF ELSE BEGIN
          hist['min'] = DOUBLE(mn)
          hist['max'] = DOUBLE(mx)
          hist['bins'] = 1L
          hist['bin_width'] = 0.0D
          hist['counts'] = LONG(N_ELEMENTS(finite))
        ENDELSE
      ENDIF
    ENDIF
  ENDIF
  ENVI_DSH_TRACE, 'inspect: hist keys=' + STRING(N_ELEMENTS(hist.Keys()))
  keys = ['raster', 'stats']
  vals = LIST()
  vals.Add, ENVI_DSH_RAW(ENVI_DSH_JHASH(desc))
  vals.Add, ENVI_DSH_RAW(ENVI_DSH_JHASH(stats))
  IF wantHistogram THEN BEGIN
    keys = [keys, 'histogram']
    vals.Add, ENVI_DSH_RAW(ENVI_DSH_JHASH(hist))
  ENDIF
  inspectJson = ENVI_DSH_JOBJ(keys, vals)
  CATCH, /CANCEL
  ENVI_DSH_TRACE, 'inspect json bytes=' + STRING(STRLEN(inspectJson))
END

;----------------------------------------------------------------------------
; Open one raster and write it out in another format.
;
; ENVI 5.2's ENVIRaster::Export accepts only 'envi', 'tiff' and 'nitf' -- there
; is no Cloud-Optimized-GeoTIFF writer here, so a 'cog' request is refused by
; the Python side rather than silently downgraded.
;----------------------------------------------------------------------------
PRO ENVI_DSH_EXPORT, rasterPath, exportPath, exportFormat
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  ENVI_DSH_TRACE, 'export: ' + STRING(rasterPath) + ' -> ' + STRING(exportPath) + ' (' + STRING(exportFormat) + ')'
  IF ~OBJ_VALID(e) THEN BEGIN
    failed = 1B
    failure = 'ENVI is not running, so no raster can be exported'
    RETURN
  ENDIF
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    em = !ERROR_STATE.MSG
    CATCH, /CANCEL
    failed = 1B
    failure = 'cannot open raster ' + STRING(rasterPath) + ': ' + STRING(em)
    RETURN
  ENDIF
  r = e.OpenRaster(STRING(rasterPath))
  IF ~OBJ_VALID(r) THEN BEGIN
    CATCH, /CANCEL
    failed = 1B
    failure = 'ENVI returned no raster for ' + STRING(rasterPath)
    RETURN
  ENDIF
  ENVI_DSH_REGISTER, r, 'EXPORT'
  err2 = ''
  r.Export, STRING(exportPath), STRING(exportFormat), ERROR = err2
  IF STRING(err2) NE '' THEN BEGIN
    CATCH, /CANCEL
    failed = 1B
    failure = 'ENVIRaster::Export failed: ' + STRING(err2)
    RETURN
  ENDIF
  IF ~FILE_TEST(STRING(exportPath)) THEN BEGIN
    CATCH, /CANCEL
    failed = 1B
    failure = 'ENVIRaster::Export reported no error but wrote no file at ' + STRING(exportPath)
    RETURN
  ENDIF
  desc = ENVI_DSH_RDESC(r, '')
  keys = ['raster', 'export_path', 'export_format']
  vals = LIST()
  vals.Add, ENVI_DSH_RAW(ENVI_DSH_JHASH(desc))
  vals.Add, ENVI_DSH_RAW(ENVI_DSH_JVAL(STRING(exportPath)))
  vals.Add, ENVI_DSH_RAW(ENVI_DSH_JVAL(STRING(exportFormat)))
  inspectJson = ENVI_DSH_JOBJ(keys, vals)
  CATCH, /CANCEL
  ENVI_DSH_TRACE, 'export ok'
END
