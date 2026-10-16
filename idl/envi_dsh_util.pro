; envi_dsh_util.pro
;
; Helpers used by envi_dsh_runner.pro. They are deliberate about a few IDL 8.4
; behaviours that are easy to get wrong when driving ENVI from outside:
;
;   * ENVI is a *function*: ``e = ENVI(/HEADLESS)`` returns the application
;     object. The runner checks it is valid before using it so a licensing
;     failure is reported as a license problem instead of crashing later.
;   * ENVIRaster has no MIN/MAX/MEAN properties (only the RasterStatistics task
;     computes those), so statistics come from GetData() + IDL MIN()/MAX()/MEAN().
;   * Undefined variables cannot be indexed or compared safely, hence the
;     N_ELEMENTS guards everywhere.
;
; Part of envimcp. Verified against IDL 8.4 + ENVI 5.2 on Windows.

;----------------------------------------------------------------------------
; Escape a string for a JSON document.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_JESC, s, VALIDATE = validate
  COMPILE_OPT IDL2
  t = STRING(s)
  t = t.Replace('\', '\\')
  t = t.Replace('"', '\"')
  t = t.Replace(STRING(9B), '\t')
  t = t.Replace(STRING(10B), '\n')
  t = t.Replace(STRING(13B), '\r')
  RETURN, t
END

;----------------------------------------------------------------------------
; JSON-encode one IDL scalar/vector.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_JVAL, v
  COMPILE_OPT IDL2
  ; Type 0 is UNDEFINED and type 1 is BYTE -- do not treat BYTE as "no value".
  IF N_ELEMENTS(v) EQ 0 THEN RETURN, 'null'
  t = SIZE(v, /TYPE)
  IF t EQ 0 THEN RETURN, 'null'
  IF t EQ 7 THEN BEGIN
    IF N_ELEMENTS(v) EQ 1 THEN RETURN, '"' + ENVI_DSH_JESC(v) + '"'
    parts = STRARR(N_ELEMENTS(v))
    FOR i = 0, N_ELEMENTS(v) - 1 DO parts[i] = '"' + ENVI_DSH_JESC(v[i]) + '"'
    RETURN, '[' + STRJOIN(parts, ',') + ']'
  ENDIF
  IF (t GE 1 AND t LE 5) OR (t GE 12 AND t LE 15) THEN BEGIN
    IF N_ELEMENTS(v) EQ 1 THEN BEGIN
      IF ISA(v, /NUMBER) THEN IF ~FINITE(v) THEN RETURN, 'null'
      RETURN, STRTRIM(v, 2)
    ENDIF
    parts = STRARR(N_ELEMENTS(v))
    FOR i = 0, N_ELEMENTS(v) - 1 DO parts[i] = STRTRIM(v[i], 2)
    RETURN, '[' + STRJOIN(parts, ',') + ']'
  ENDIF
  IF t EQ 9 THEN BEGIN
    parts = STRARR(N_ELEMENTS(v))
    FOR i = 0, N_ELEMENTS(v) - 1 DO parts[i] = '"' + ENVI_DSH_JESC(v[i]) + '"'
    RETURN, '[' + STRJOIN(parts, ',') + ']'
  ENDIF
  RETURN, '"' + ENVI_DSH_JESC(STRTRIM(COMPLEX(v), 2)) + '"'
END

;----------------------------------------------------------------------------
; Read a whole text file as one string ('' when unreadable).
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_READFILE, path
  COMPILE_OPT IDL2
  text = ''
  IF ~FILE_TEST(path) THEN RETURN, text
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    RETURN, ''
  ENDIF
  OPENR, lun, path, /GET_LUN
  line = ''
  WHILE ~EOF(lun) DO BEGIN
    READF, lun, line
    text = text + line
  ENDWHILE
  FREE_LUN, lun
  CATCH, /CANCEL
  RETURN, text
END

;----------------------------------------------------------------------------
; JSON array of the per-step records written by the generated step modules:
; ``gen/<job>_step0.json`` ... ``gen/<job>_step<n-1>.json``.
;
; Assembled by reading the files back instead of accumulating in memory because
; IDL 8.4 cannot build an array of HASH objects (HASH() has no OBJ_NEW
; equivalent), and generated code cannot index nested literals either.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_JSTEPS, jobToken, count
  COMPILE_OPT IDL2
  COMMON ENVI_DSH_RUN_COMMON, e, appStarted, enviVersion, errCount, startupErr, $
                              rasters, rasterNames, rasterCount, $
                              failed, failure, logFile, progressFile, $
                              statusFile, outDir, jobId, opName, $
                              inspectJson
  genDir = FILE_DIRNAME(STRING(statusFile)) + '\gen'
  n = LONG(count)
  IF n LE 0 THEN RETURN, '[]'
  parts = STRARR(n)
  FOR i = 0, n - 1 DO BEGIN
    path = genDir + '\' + STRING(jobToken) + '_step' + STRTRIM(i, 2) + '.json'
    text = ENVI_DSH_READFILE(path)
    IF text EQ '' THEN BEGIN
      ; Fall back to the file name without the job prefix, so a renamed or
      ; hand-authored step record is still picked up instead of vanishing.
      alt = genDir + '\step' + STRTRIM(i, 2) + '.json'
      text = ENVI_DSH_READFILE(alt)
      path = alt
    ENDIF
    IF text EQ '' THEN text = '{"status":"failed","index":' + STRTRIM(i, 2) + $
                               ',"error":"the step wrote no result file"}'
    parts[i] = text
  ENDFOR
  RETURN, '[' + STRJOIN(parts, ',') + ']'
END

FUNCTION ENVI_DSH_N, v
  COMPILE_OPT IDL2
  n = N_ELEMENTS(v)
  IF n LE 0 THEN RETURN, 0
  RETURN, n
END

;----------------------------------------------------------------------------
; JSON object from a HASH.
;
; This exists because IDL 8.4 cannot index a nested literal expression such as
; ``list[0]['key']`` (syntax error) and because the hand-rolled encoder in
; ENVI_DSH_JVAL only knows scalars and vectors. Values that are *already*
; rendered JSON text are passed in as single-element STRING arrays and are
; emitted verbatim, which is how nested objects are composed without recursion.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_RAW, s
  COMPILE_OPT IDL2
  RETURN, [STRING(s)]
END

;----------------------------------------------------------------------------
; JSON object from parallel key/value arrays.
;
; The generated modules use this instead of building a HASH: HASH() cannot be
; referenced from code compiled by ``.compile`` inside an ``idl.exe -e`` batch
; file, where the call fails with
; "% Attempt to call undefined function: 'HASH'".
;
; VALUE HANDLING: a SCALAR is encoded with ENVI_DSH_JVAL, so a STRING value
; becomes a JSON string (paths, task names). An ARRAY -- which here only ever
; comes from ENVI_DSH_RAW / ENVI_DSH_JOBJ / ENVI_DSH_JHASH / ENVI_DSH_JSTEPS --
; is treated as one already-rendered JSON fragment and emitted verbatim. That
; split exists because a scalar STRING is exactly what must be quoted, while a
; nested object must not be.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_JOBJ, keys, values
  COMPILE_OPT IDL2
  n = N_ELEMENTS(keys)
  IF n LE 0 THEN RETURN, '{}'
  ; Truncate to whichever parallel list is shorter. Truncating only the count
  ; would emit a dangling '"key":' pair and produce invalid JSON, so the loop
  ; bound alone is not enough -- the parts array must shrink with it.
  IF N_ELEMENTS(values) LT n THEN n = N_ELEMENTS(values)
  IF n LE 0 THEN RETURN, '{}'
  parts = STRARR(n)
  FOR i = 0, n - 1 DO BEGIN
    v = values[i]
    IF N_ELEMENTS(v) EQ 0 THEN BEGIN
      parts[i] = '"' + ENVI_DSH_JESC(STRING(keys[i])) + '":null'
    ENDIF ELSE IF ISA(v, /ARRAY) THEN BEGIN
      parts[i] = '"' + ENVI_DSH_JESC(STRING(keys[i])) + '":' + STRING(v)
    ENDIF ELSE BEGIN
      parts[i] = '"' + ENVI_DSH_JESC(STRING(keys[i])) + '":' + ENVI_DSH_JVAL(v)
    ENDELSE
  ENDFOR
  IF n EQ 1 THEN RETURN, '{' + parts[0] + '}'
  RETURN, '{' + STRJOIN(parts, ',') + '}'
END

FUNCTION ENVI_DSH_JHASH, h
  COMPILE_OPT IDL2
  IF ~ISA(h, 'HASH') THEN RETURN, '{}'
  keys = h.Keys()
  ; An EMPTY hash must return here. STRARR(0) is not a legal array in IDL 8.4 --
  ; it aborts with "% Array dimensions must be greater than 0." and takes the
  ; whole job down, which is exactly what happened when a raster description or
  ; statistics hash came back empty for a virtual (never-written) output.
  n = N_ELEMENTS(keys)
  IF n LE 0 THEN RETURN, '{}'
  parts = STRARR(n)
  count = 0
  FOR i = 0, N_ELEMENTS(keys) - 1 DO BEGIN
    k = STRING(keys[i])
    IF k EQ '' THEN CONTINUE
    v = h[k]
    IF ISA(v, 'HASH') THEN v = [ENVI_DSH_JHASH(v)]
    IF ~ISA(v, /ARRAY) THEN BEGIN
      ; A non-array scalar can only be plain data: ENVI_DSH_RAW returns an array
      ; by construction, so pre-rendered JSON never reaches this branch.
      parts[count] = '"' + ENVI_DSH_JESC(k) + '":' + ENVI_DSH_JVAL(v)
    ENDIF ELSE IF ISA(v, 'STRING') THEN BEGIN
      ; An array of strings is already-rendered JSON: emitted verbatim so the
      ; fragment does not come back quoted (which produced `"uri":E:/...`).
      parts[count] = '"' + ENVI_DSH_JESC(k) + '":' + STRING(v[0])
    ENDIF ELSE IF ISA(v, 'LIST') THEN BEGIN
      parts[count] = '"' + ENVI_DSH_JESC(k) + '":[' + ENVI_DSH_JLIST(v) + ']'
    ENDIF ELSE BEGIN
      ; NOTE: a zero-length array is never stored in a HASH -- IDL aborts with
      ; '% Array dimensions must be greater than 0.' on the assignment itself.
      parts[count] = '"' + ENVI_DSH_JESC(k) + '":' + ENVI_DSH_JVAL(v)
    ENDELSE
    count += 1
  ENDFOR
  IF count EQ 0 THEN RETURN, '{}'
  IF count EQ 1 THEN RETURN, '{' + parts[0] + '}'
  RETURN, '{' + STRJOIN(parts[0:count-1], ',') + '}'
END

;----------------------------------------------------------------------------
; JSON array from a LIST of values.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_JLIST, l
  COMPILE_OPT IDL2
  IF ~ISA(l, 'LIST') THEN RETURN, ''
  n = l.Count()
  IF n EQ 0 THEN RETURN, ''
  parts = STRARR(n)
  FOR i = 0, n - 1 DO BEGIN
    v = l[i]
    IF ISA(v, 'HASH') THEN v = [ENVI_DSH_JHASH(v)]
    IF ~ISA(v, /ARRAY) THEN BEGIN
      ; Plain data: ENVI_DSH_RAW is an array by construction, so a scalar here
      ; is never a pre-rendered fragment.
      parts[i] = ENVI_DSH_JVAL(v)
    ENDIF ELSE IF ISA(v, 'STRING') THEN BEGIN
      parts[i] = STRING(v[0])
    ENDIF ELSE IF ISA(v, 'LIST') THEN BEGIN
      parts[i] = '[' + ENVI_DSH_JLIST(v) + ']'
    ENDIF ELSE BEGIN
      parts[i] = ENVI_DSH_JVAL(v)
    ENDELSE
  ENDFOR
  RETURN, STRJOIN(parts, ',')
END

;----------------------------------------------------------------------------
; ENVI data type code -> readable name.
;
; ENVI's codes and IDL's own SIZE() codes are NOT the same, so this is a
; hand-written table: ENVI 3 is long (IDL 3 is also long, but ENVI 6 is complex
; where IDL 6 is complex too yet ENVI 9 is complex double while IDL 9 is dcomplex)
; -- keeping it explicit avoids a silent mismatch.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_TYPENAME, code
  COMPILE_OPT IDL2
  IF N_ELEMENTS(code) EQ 0 THEN RETURN, 'unknown'
  IF ~ISA(code, /NUMBER) THEN RETURN, STRING(code)
  c = LONG(code)
  IF c EQ 0 THEN RETURN, 'undefined'
  IF c EQ 1 THEN RETURN, 'byte'
  IF c EQ 2 THEN RETURN, 'int'
  IF c EQ 3 THEN RETURN, 'long'
  IF c EQ 4 THEN RETURN, 'float'
  IF c EQ 5 THEN RETURN, 'double'
  IF c EQ 6 THEN RETURN, 'complex'
  IF c EQ 9 THEN RETURN, 'complex double'
  IF c EQ 12 THEN RETURN, 'uint'
  IF c EQ 13 THEN RETURN, 'ulong'
  IF c EQ 14 THEN RETURN, 'long64'
  IF c EQ 15 THEN RETURN, 'ulong64'
  RETURN, 'type ' + STRTRIM(c, 2)
END

;----------------------------------------------------------------------------
; ENVI's ``DATA_TYPE`` answers a *name* (e.g. 'uint'), not a code. This maps it
; to the classic numeric ENVI type code so status.json keeps the same shape as
; the .hdr files and the documented codes.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_TYPE_CODE, value
  COMPILE_OPT IDL2
  IF N_ELEMENTS(value) EQ 0 THEN RETURN, 0L
  IF ISA(value, /NUMBER) THEN RETURN, LONG(value)
  s = STRUPCASE(STRTRIM(STRING(value), 2))
  IF s EQ 'BYTE' THEN RETURN, 1L
  IF s EQ 'INT' THEN RETURN, 2L
  IF s EQ 'LONG' THEN RETURN, 3L
  IF s EQ 'FLOAT' THEN RETURN, 4L
  IF s EQ 'DOUBLE' THEN RETURN, 5L
  IF s EQ 'COMPLEX' THEN RETURN, 6L
  IF s EQ 'COMPLEX DOUBLE' THEN RETURN, 9L
  IF s EQ 'UINT' THEN RETURN, 12L
  IF s EQ 'ULONG' THEN RETURN, 13L
  IF s EQ 'LONG64' THEN RETURN, 14L
  IF s EQ 'ULONG64' THEN RETURN, 15L
  RETURN, 0L
END

;----------------------------------------------------------------------------
; The raw ``DATA_TYPE`` property (a name such as 'uint', not a code).
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_RAW_TYPE, raster
  COMPILE_OPT IDL2
  IF ~OBJ_VALID(raster) THEN RETURN, ''
  value = ''
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    RETURN, ''
  ENDIF
  value = raster.DATA_TYPE
  CATCH, /CANCEL
  RETURN, value
END

;----------------------------------------------------------------------------
; Safe numeric property read from a raster handle.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_NUM, raster, prop, default
  COMPILE_OPT IDL2
  IF ~OBJ_VALID(raster) THEN RETURN, default
  value = default
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    RETURN, default
  ENDIF
  IF prop EQ 'NROWS' THEN value = raster.NROWS
  IF prop EQ 'NCOLUMNS' THEN value = raster.NCOLUMNS
  IF prop EQ 'NBANDS' THEN value = raster.NBANDS
  IF prop EQ 'DATA_TYPE' THEN value = raster.DATA_TYPE
  IF prop EQ 'BYTE_ORDER' THEN value = raster.BYTE_ORDER
  CATCH, /CANCEL
  RETURN, value
END

;----------------------------------------------------------------------------
; Safe string property read from a raster handle.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_STR, raster, prop, default
  COMPILE_OPT IDL2
  IF ~OBJ_VALID(raster) THEN RETURN, default
  value = default
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    RETURN, default
  ENDIF
  IF prop EQ 'URI' THEN value = raster.URI
  IF prop EQ 'INTERLEAVE' THEN value = raster.INTERLEAVE
  IF prop EQ 'FILETYPE' THEN value = raster.FILETYPE
  IF prop EQ 'NAME' THEN value = raster.NAME
  CATCH, /CANCEL
  RETURN, STRING(value)
END

;----------------------------------------------------------------------------
; Pixel size of the first band, as a string ('' when unavailable).
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_UNITS, raster
  COMPILE_OPT IDL2
  IF ~OBJ_VALID(raster) THEN RETURN, ''
  value = ''
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    RETURN, ''
  ENDIF
  value = raster.PIXEL_SIZE
  CATCH, /CANCEL
  IF N_ELEMENTS(value) EQ 0 THEN RETURN, ''
  RETURN, STRING(value[0])
END

;----------------------------------------------------------------------------
; Statistics over the finite pixels of a band. Keys: min / max / mean / stddev /
; sample_count / finite_count, or min_<label>, ... for a labelled band.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_FILLSTATS, stats, data, label
  COMPILE_OPT IDL2
  IF N_ELEMENTS(data) EQ 0 THEN RETURN, stats
  finite = FINITE(data)
  nFinite = TOTAL(finite)
  stats['sample_count'] = LONG64(N_ELEMENTS(data))
  stats['finite_count'] = LONG64(nFinite)
  IF nFinite LE 0 THEN RETURN, stats
  d = data[WHERE(finite)]
  mn = DOUBLE(MIN(d))
  mx = DOUBLE(MAX(d))
  dMean = DOUBLE(MEAN(d))
  sd = 0.0D
  IF N_ELEMENTS(d) GT 1 THEN sd = DOUBLE(SQRT(TOTAL((d - dMean)^2) / (N_ELEMENTS(d) - 1)))
  IF label NE '' THEN BEGIN
    stats['min_' + label] = mn
    stats['max_' + label] = mx
    stats['mean_' + label] = dMean
    stats['stddev_' + label] = sd
  ENDIF ELSE BEGIN
    stats['min'] = mn
    stats['max'] = mx
    stats['mean'] = dMean
    stats['stddev'] = sd
  ENDELSE
  RETURN, stats
END

FUNCTION ENVI_DSH_RSTATS, raster
  COMPILE_OPT IDL2
  stats = HASH()
  IF ~OBJ_VALID(raster) THEN RETURN, stats
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    stats['stats_error'] = !ERROR_STATE.MSG
    RETURN, stats
  ENDIF
  nb = LONG(ENVI_DSH_NUM(raster, 'NBANDS', 1))
  data = raster.GetData()
  IF N_ELEMENTS(data) EQ 0 THEN BEGIN
    CATCH, /CANCEL
    stats['stats_error'] = 'raster contains no pixels'
    RETURN, stats
  ENDIF
  dims = SIZE(data, /N_DIMENSIONS)
  IF nb GT 1 AND dims EQ 3 THEN BEGIN
    FOR b = 0, nb - 1 DO stats = ENVI_DSH_FILLSTATS(stats, data[*, *, b], STRTRIM(b + 1, 2))
  ENDIF ELSE BEGIN
    stats = ENVI_DSH_FILLSTATS(stats, data, '')
  ENDELSE
  CATCH, /CANCEL
  RETURN, stats
END

;----------------------------------------------------------------------------
; Describe one raster (or one element of a raster series) as a HASH.
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_RDESC, raster, label
  COMPILE_OPT IDL2
  out = HASH()
  IF ~OBJ_VALID(raster) THEN RETURN, out
  out['parameter'] = label
  out['uri'] = ENVI_DSH_STR(raster, 'URI', '')
  out['nrows'] = LONG64(ENVI_DSH_NUM(raster, 'NROWS', 0))
  out['ncolumns'] = LONG64(ENVI_DSH_NUM(raster, 'NCOLUMNS', 0))
  out['nbands'] = LONG64(ENVI_DSH_NUM(raster, 'NBANDS', 0))
  dt = ENVI_DSH_TYPE_CODE(ENVI_DSH_RAW_TYPE(raster))
  out['data_type'] = dt
  out['data_type_name'] = ENVI_DSH_TYPENAME(dt)
  out['interleave'] = ENVI_DSH_STR(raster, 'INTERLEAVE', '')
  out['byte_order'] = LONG(ENVI_DSH_NUM(raster, 'BYTE_ORDER', 0))
  out['file_type'] = ENVI_DSH_STR(raster, 'FILETYPE', '')
  out['name'] = ENVI_DSH_STR(raster, 'NAME', '')
  out['pixel_size'] = ENVI_DSH_UNITS(raster)
  RETURN, out
END

;----------------------------------------------------------------------------
; Which output parameters carry rasters (single or series).
;----------------------------------------------------------------------------
FUNCTION ENVI_DSH_ISRASTERPARAM, name
  COMPILE_OPT IDL2
  rasterParams = ['OUTPUT_RASTER', 'OUTPUT_RASTER1', 'OUTPUT_RASTER2', 'OUTPUT_MEANS_RASTER', $
                  'OUTPUT_RASTERSERIES', 'OUTPUT_RULE_RASTER']
  FOR i = 0, N_ELEMENTS(rasterParams) - 1 DO IF name EQ rasterParams[i] THEN RETURN, 1
  RETURN, 0
END

;----------------------------------------------------------------------------
; Create a directory and its parents if it does not exist; never fatal.
;
; The generated step procedures create their own output parents, but a
; step-less job (INSPECT_RASTER / EXPORT_RASTER) has no step to do it, and
; without the job directory the serializer cannot write status.json at all --
; which surfaces as a job that dies silently with no diagnostics.
;----------------------------------------------------------------------------
PRO ENVI_DSH_MAKEDIR, path
  COMPILE_OPT IDL2
  IF STRING(path) EQ '' THEN RETURN
  IF FILE_TEST(STRING(path), /DIRECTORY) THEN RETURN
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    RETURN
  ENDIF
  FILE_MKDIR, STRING(path)
  CATCH, /CANCEL
END

;----------------------------------------------------------------------------
; Best-effort write of a text file (status and per-step JSON).
;----------------------------------------------------------------------------
PRO ENVI_DSH_WRITEFILE, path, text, OK = ok, ERRMSG = errmsg
  COMPILE_OPT IDL2
  ok = 0
  errmsg = ''
  tmp = path + '.tmp'
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    errmsg = !ERROR_STATE.MSG
    RETURN
  ENDIF
  OPENW, lun, tmp, /GET_LUN
  PRINTF, lun, text
  FREE_LUN, lun
  FILE_MOVE, tmp, path, /OVERWRITE
  ok = 1
  CATCH, /CANCEL
END

;----------------------------------------------------------------------------
; Append one progress event; never fatal.
;----------------------------------------------------------------------------
PRO ENVI_DSH_PUSH, progressfile, event
  COMPILE_OPT IDL2
  IF progressfile EQ '' THEN RETURN
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    RETURN
  ENDIF
  OPENU, lun, progressfile, /GET_LUN, /APPEND
  PRINTF, lun, event
  FREE_LUN, lun
  CATCH, /CANCEL
END

;----------------------------------------------------------------------------
; Append a log line to the IDL-side log file; never fatal.
;----------------------------------------------------------------------------
PRO ENVI_DSH_LOG, logfile, text
  COMPILE_OPT IDL2
  IF logfile EQ '' THEN RETURN
  CATCH, catchErr
  IF catchErr NE 0 THEN BEGIN
    CATCH, /CANCEL
    RETURN
  ENDIF
  OPENU, lun, logfile, /GET_LUN, /APPEND
  PRINTF, lun, text
  FREE_LUN, lun
  CATCH, /CANCEL
END
