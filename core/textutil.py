"""Strip emoji / non-encodable chars for Windows console + MSSQL safety."""

from __future__ import annotations

import re
from typing import Any

# Astral-plane emoji + flags + variation selectors / ZWJ.
# BMP ranges (U+2600-U+27BF) are deliberately excluded: they also hold
# text symbols such as checkmarks, arrows and dingbats that are real data.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"  # ZWJ
    "]+",
    flags=re.UNICODE,
)

# Collapse leftover spaces left by removals
_SPACE_RE = re.compile(r"[ \t]{2,}")


def strip_emoji(text: str) -> str:
    cleaned = _EMOJI_RE.sub("", text)
    if cleaned == text:
        # Nothing removed — return the value untouched so no data is altered.
        return text
    return _SPACE_RE.sub(" ", cleaned).strip()


def safe_console(text: str) -> str:
    """Make text printable on Windows cp125x consoles."""
    cleaned = strip_emoji(text)
    try:
        cleaned.encode("cp1254")
        return cleaned
    except UnicodeEncodeError:
        return cleaned.encode("cp1254", errors="replace").decode("cp1254")


def utf16_len(text: str) -> int:
    """NVARCHAR(n) counts UTF-16 code units; astral chars occupy two."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def clip_utf16(text: str, max_units: int) -> str:
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > max_units:
            return text[:i]
    return text


def as_clean_str(value: Any, max_len: int | None = None) -> str | None:
    """
    Emoji-stripped string. `max_len` clips to the target column width so
    narrowed NVARCHAR columns cannot raise "String data, right truncation".
    Clipping is UTF-16 aware to match SQL Server's NVARCHAR length semantics.
    """
    if value is None:
        return None
    cleaned = strip_emoji(str(value))
    if max_len is not None:
        return clip_utf16(cleaned, max_len)
    return cleaned
