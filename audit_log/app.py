"""The companion audit-log service (docs/context.md section 7.5).

Records every AWS action CloudCopilot takes and every fallback-suggestion
reply, and lets that trail be reviewed. Deliberately just a plain FastAPI app
here — Docker Compose wiring (the actual second container, on a custom
network with the agent) is its own later phase; this is tested directly and
run locally with uvicorn in the meantime.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from audit_log.storage import EventStore

app = FastAPI(title="CloudCopilot Audit Log")
store = EventStore()

DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "dashboard.html"


class EventIn(BaseModel):
    question: str
    question_id: str
    path: str
    timestamp: str
    duration_seconds: float
    success: bool
    service: str | None = None
    operation: str | None = None
    params: dict | None = None
    result_count: int | None = None
    preview: str | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/events", status_code=201)
def create_event(event: EventIn) -> dict:
    event_id = store.insert(event.model_dump())
    return {"id": event_id}


@app.get("/events")
def list_events(
    question_id: str | None = None,
    path: str | None = None,
    service: str | None = None,
    success: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    return store.query(
        question_id=question_id, path=path, service=service, success=success, limit=limit
    )


@app.get("/stats")
def stats(
    path: str | None = None,
    service: str | None = None,
    success: bool | None = None,
) -> dict:
    """Aggregates behind the dashboard (see /dashboard) — same filters as /events,
    so a filtered dashboard view and the event table underneath it always agree."""
    return store.stats(path=path, service=service, success=success)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    """A read-only view of the audit trail (docs/context.md section 7.5) — fetches
    /stats and /events client-side; adds nothing the API doesn't already expose."""
    return DASHBOARD_HTML_PATH.read_text()
