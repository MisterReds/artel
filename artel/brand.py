"""Artel's terminal identity: the copper mark and colours, matching the dashboard (docs/BRAND.md).

Respects NO_COLOR and non-terminals: plain text then."""

from __future__ import annotations

import os
import sys

COPPER = (181, 82, 43)
MUTED = (110, 102, 92)
STATUS = {"done": (46, 133, 73), "running": (31, 122, 114), "review": (122, 85, 201), "blocked": (192, 57, 43),
          "ready": (176, 127, 18), "todo": (163, 158, 150)}


def _on(stream=None) -> bool:
    stream = stream or sys.stdout
    return not os.environ.get("NO_COLOR") and hasattr(stream, "isatty") and stream.isatty()


def paint(text: str, rgb=COPPER, bold: bool = False, stream=None) -> str:
    if not _on(stream):
        return text
    return f"\033[{'1;' if bold else ''}38;2;{rgb[0]};{rgb[1]};{rgb[2]}m{text}\033[0m"


def mark(stream=None) -> str:
    """The A-frame mark in one glyph run: ▲ in copper."""
    return paint("▲", COPPER, bold=True, stream=stream)


def banner(subtitle: str = "", stream=None) -> str:
    head = f"{mark(stream)} {paint('Artel', COPPER, bold=True, stream=stream)}"
    return head + (f"  {paint(subtitle, MUTED, stream=stream)}" if subtitle else "")


def status(word: str, stream=None) -> str:
    return paint(word, STATUS.get(word, MUTED), stream=stream)
