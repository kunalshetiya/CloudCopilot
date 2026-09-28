"""Terminal styling for the interactive/single-question CLI — purely a
presentation layer, applied only in agent/cli.py at the point of printing.

Deliberately NOT applied inside agent/router.py's own ``_format_*``
functions: those return plain, testable text (`assert "1 EC2 instance(s)..."
in result` — unit tests all over tests/test_router.py depend on that), and
they're the one place any future consumer other than this CLI would read an
answer from. Keeping them plain keeps that true; this module is a separate,
later step that only the CLI applies.

Colored output is skipped automatically — never a special case a caller has
to opt into — whenever the destination isn't a real terminal (a pipe, a
Docker log with no TTY attached, every existing test's StringIO/capsys
stream) or the widely-respected ``NO_COLOR`` convention
(https://no-color.org) is set. No new dependency: plain ANSI escape codes,
which every terminal this agent's own supported platforms use natively.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"

BULLET = "•"


def _color_enabled(stream: TextIO) -> bool:
    if "NO_COLOR" in os.environ:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _wrap(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET


def format_answer(answer: str, stream: TextIO = sys.stdout) -> str:
    """Bold the answer's headline (its first line) and give '- ' bullet
    lines (agent/router.py's own ``_bullets()`` convention) a nicer marker.
    Purely structural — it doesn't know or care what any formatter's output
    means, just that a leading '- ' is always a bullet and a first line is
    always a headline, true across every formatter in the registry and
    dispatch's own generic one alike."""
    if not answer or not _color_enabled(stream):
        return answer
    lines = answer.split("\n")
    styled = []
    for i, line in enumerate(lines):
        if line.startswith("- "):
            styled.append(f"  {_wrap(BULLET, CYAN)} {line[2:]}")
        elif i == 0:
            styled.append(_wrap(line, BOLD))
        else:
            styled.append(line)
    return "\n".join(styled)


def prompt(stream: TextIO = sys.stdout) -> str:
    return _wrap("❯ ", BOLD, CYAN) if _color_enabled(stream) else "> "


def banner(text: str, stream: TextIO = sys.stdout) -> str:
    return _wrap(text, BOLD, CYAN) if _color_enabled(stream) else text
