"""envimcp -- drive ENVI 5.2 operators from an AI agent over MCP.

Architecture (verified on this machine):

    MCP client  ->  envimcp server (Python)  ->  idl.exe -e @job.pro  ->  ENVI(/HEADLESS)

`envipyengine` is NOT used: it drives the IDL Task Engine, which first ships in
IDL 8.5.2 / ENVI 5.3. IDL 8.4 has neither that engine nor an IDL-Python bridge,
so envimcp spawns one headless IDL process per job and exchanges typed IDL
literals plus a JSON result file.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
