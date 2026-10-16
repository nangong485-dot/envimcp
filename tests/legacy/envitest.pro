; envitest.pro - headless ENVI 5.2 + ENVITask verification (proven working)
; <repo> = absolute path of the envimcp checkout; substitute it before running.
PRO envitest
  COMPILE_OPT IDL2

  e = ENVI(/HEADLESS)
  PRINT, 'ENVI version: ' + e.VERSION
  PRINT, 'WIDGET_ID (headless => 0): ' + STRTRIM(e.WIDGET_ID, 2)
  PRINT, 'Task count: ' + STRTRIM(N_ELEMENTS(e.TASK_NAMES), 2)

  raster = e.OpenRaster('D:\ENVI52\data\qb_boulder_msi')
  PRINT, 'Raster: ' + STRTRIM(raster.NCOLUMNS,2) + ' x ' + STRTRIM(raster.NROWS,2) + $
    ' x ' + STRTRIM(raster.NBANDS,2) + ' bands'

  task = ENVITask('SpectralIndex')
  task.INPUT_RASTER = raster
  task.INDEX = 'Normalized Difference Vegetation Index'
  task.OUTPUT_RASTER_URI = '<repo>\out_ndvi.dat'
  task.Execute
  PRINT, 'Task executed. OUTPUT_RASTER = ' + task.OUTPUT_RASTER.URI

  e.Close
  PRINT, 'HEADLESS_TASK_OK'
  EXIT, STATUS=0
END
