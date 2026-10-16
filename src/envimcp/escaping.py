"""Text escaping and formatting helpers shared by the IDL and JSON writers.

Everything envimcp hands to IDL goes through here, so paths containing spaces
(this workspace is ``E:\\aiworkspace\\deep seekharness\\envi``), apostrophes,
CJK characters or backslashes survive round-tripping.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "idl_string",
    "idl_quote_path",
    "escape_log_message",
    "json_dumps",
    "safe_log_text",
    "is_probably_utf8",
]


def idl_string(value: str) -> str:
    """Render *value* as an IDL string literal.

    IDL string literals accept embedded double quotes without escaping, so any
    apostrophe in the value is emitted as a ``"`` character instead of ``''``.
    Backslashes are literal in IDL and are passed through unchanged, which is
    what Windows paths need.
    """
    text = str(value)
    if '"' in text:
        # A value containing both quote kinds cannot be represented safely in a
        # single literal; collapse double quotes to apostrophes and double the
        # apostrophes so the literal stays unambiguous.
        text = text.replace('"', "'").replace("'", "''")
        return f"'{text}'"
    return "'" + text + "'"


def idl_quote_path(path: str) -> str:
    """Like :func:`idl_string` but normalises separators first.

    IDL accepts ``/`` on Windows and it keeps generated scripts free of escape
    ambiguity, so separators are converted.
    """
    return idl_string(str(path).replace("\\", "/"))


def escape_log_message(value: str) -> str:
    """Escape a value for embedding into an IDL single-quoted literal in a log line."""
    return str(value).replace("'", "' + STRING(39B) + '").replace("\r", " ").replace("\n", " ")


def json_dumps(obj: Any, *, indent: int | None = 2) -> str:
    """UTF-8-safe JSON with forward-slash-free backslash escaping left to json."""
    return json.dumps(obj, indent=indent, ensure_ascii=False, sort_keys=False)


def safe_log_text(value: Any, limit: int = 4000) -> str:
    """Normalise a captured stdout/stderr blob for handing back to an agent."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > limit:
        half = limit // 2
        return text[:half] + f"\n... [{len(text) - limit} chars omitted] ...\n" + text[-half:]
    return text


def is_probably_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
