"""CLI entry point — single-question and interactive modes (docs/context.md 7.1).

Run ``python -m agent "<question>"`` for one question, or ``python -m agent``
with no arguments to start an interactive session (used for the demo's 10
questions).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from typing import TextIO

from agent import style
from agent.audit import AuditLog, FileAuditLog, HttpAuditLog
from agent.router import answer_question

EXIT_COMMANDS = {"exit", "quit"}


def _default_audit_log() -> AuditLog:
    """FileAuditLog stays the zero-dependency default for plain local runs.

    Set AUDIT_LOG_URL to switch to the real service instead — this is how
    Docker Compose wires the two containers together (docs/context.md 7.5),
    without changing anything about how the CLI behaves outside of it.

    This split is deliberate (7.5), but silent — events logged here never
    reach the audit-log service's dashboard (audit_log/dashboard.html), which
    only ever sees what HttpAuditLog posts. Printed once per run rather than
    left to surprise whoever later wonders why a question they asked doesn't
    show up there (docs/context.md section 8).
    """
    url = os.environ.get("AUDIT_LOG_URL")
    if url:
        return HttpAuditLog(base_url=url)
    print(
        "note: logging locally to audit_log/events.jsonl — this won't appear on the "
        "audit dashboard. Run via Docker Compose, or set AUDIT_LOG_URL, to use the "
        "centralized audit-log service instead.",
        file=sys.stderr,
    )
    return FileAuditLog()


def run_single_question(question: str, *, audit_log: AuditLog | None = None) -> str:
    return answer_question(question, audit_log=audit_log or _default_audit_log())


def run_interactive(
    lines: Iterable[str], out: TextIO = sys.stdout, *, audit_log: AuditLog | None = None
) -> None:
    audit_log = audit_log or _default_audit_log()
    print(style.prompt(out), end="", file=out, flush=True)
    for line in lines:
        question = line.strip()
        if not question:
            print(style.prompt(out), end="", file=out, flush=True)
            continue
        if question.lower() in EXIT_COMMANDS:
            break
        answer = answer_question(question, audit_log=audit_log)
        print(style.format_answer(answer, out), file=out)
        print(file=out)
        print(style.prompt(out), end="", file=out, flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print(style.format_answer(run_single_question(" ".join(argv))))
        return 0
    print(style.banner("CloudCopilot interactive mode. Type a question, or 'exit' to quit."))
    run_interactive(sys.stdin)
    return 0
