"""The audit-log client seam (docs/context.md section 7.5).

The real audit-log service (``audit_log/`` — a FastAPI + SQLite app, meant to run
as its own container per the mission's two-service Docker Compose requirement) is
reached through ``HttpAuditLog``. ``FileAuditLog`` is a local, no-network stand-in
implementing the same ``AuditLog`` interface — useful for quick local runs without
the service running. Either way, ``agent/router.py`` never changes: only which
``AuditLog`` implementation gets constructed at startup.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

# "registry": a fixed-registry capability answered it, with one or more real AWS
# calls behind it. "fallback": reserved for structured dynamic dispatch, deferred
# past v1 (context.md 7.2 tier 2) — unused until that's built. "suggestion": no
# AWS call at all, just the AI's plain-language pointer (7.2 tier 3).
AnswerPath = Literal["registry", "fallback", "suggestion"]


@dataclass(frozen=True)
class AuditEvent:
    """One recorded interaction, per the fields decided in context.md 7.5.

    For the "registry" path, one ``AuditEvent`` is recorded per real AWS call —
    not per question — since a single question can trigger several distinct
    calls (e.g. one per S3 bucket). ``question_id`` links all of them back to
    the one question that triggered them. The "suggestion" path makes no AWS
    call at all, so ``service``/``operation``/``params``/``result_count``/
    ``preview`` stay ``None`` for it.
    """

    question: str
    question_id: str
    path: AnswerPath
    timestamp: str
    duration_seconds: float
    success: bool
    service: str | None = None
    operation: str | None = None
    params: dict[str, Any] | None = None
    result_count: int | None = None
    preview: str | None = None


class AuditLog(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class FileAuditLog:
    """Appends one JSON line per event to a local file."""

    def __init__(self, path: Path | str = "audit_log/events.jsonl"):
        self.path = Path(path)

    def record(self, event: AuditEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(event)) + "\n")


class HttpAuditLog:
    """Sends each event to the real audit-log service over HTTP.

    Recording a call must never be allowed to break the agent's actual job of
    answering the question — a slow or unreachable audit-log service degrades
    to "this one action didn't get logged," not "the agent stopped working."
    So failures are caught and reported to stderr, never raised, and a short
    timeout keeps one bad request from stalling every subsequent AWS call.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8001",
        timeout: float = 2.0,
        client: Any | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx

    def record(self, event: AuditEvent) -> None:
        try:
            response = self._client.post(
                f"{self.base_url}/events", json=asdict(event), timeout=self.timeout
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            print(f"warning: failed to record audit event: {e}", file=sys.stderr)
