"""SQLite-backed storage for audit events.

A plain file-based database, per docs/context.md section 7.5 — no separate
database server, so the two-service (agent + audit-log) Docker Compose
requirement doesn't need a third container just for this. One connection is
opened and closed per call rather than pooled — simple, and plenty for the
call volume this project actually has.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

# Overridable so a container can point this at a separate, volume-mounted
# directory (e.g. /data/audit.db) instead of alongside the code — a volume
# mounted at the code's own directory would hide app.py/storage.py entirely,
# replacing them with the (initially empty) volume's contents.
DEFAULT_DB_PATH = Path(
    os.environ.get("AUDIT_LOG_DB_PATH", str(Path(__file__).resolve().parent / "audit.db"))
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    question_id TEXT NOT NULL,
    path TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    success INTEGER NOT NULL,
    service TEXT,
    operation TEXT,
    params TEXT,
    result_count INTEGER,
    preview TEXT
)
"""


class EventStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        conn = self._connect()
        conn.execute(SCHEMA)
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _raw_query(conn: sqlite3.Connection, params: list[Any]):
        """The one place a query/stats method actually executes SQL — every caller
        builds its own WHERE clause first, then runs it through here with `params`
        bound via `?` placeholders, never concatenated. False positive: this isn't
        SQLAlchemy, and the SQL text is built only from our own hardcoded literal
        clauses, never caller-controlled text."""

        def run(sql: str, extra_params: tuple[Any, ...] = ()):
            return conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query  # noqa: E501
                sql, (*params, *extra_params)
            )

        return run

    def insert(self, event: dict[str, Any]) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO events
                    (question, question_id, path, timestamp, duration_seconds, success,
                     service, operation, params, result_count, preview)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["question"],
                    event["question_id"],
                    event["path"],
                    event["timestamp"],
                    event["duration_seconds"],
                    int(event["success"]),
                    event.get("service"),
                    event.get("operation"),
                    json.dumps(event["params"]) if event.get("params") is not None else None,
                    event.get("result_count"),
                    event.get("preview"),
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def query(
        self,
        question_id: str | None = None,
        path: str | None = None,
        service: str | None = None,
        success: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if question_id is not None:
            clauses.append("question_id = ?")
            params.append(question_id)
        if path is not None:
            clauses.append("path = ?")
            params.append(path)
        if service is not None:
            clauses.append("service = ?")
            params.append(service)
        if success is not None:
            clauses.append("success = ?")
            params.append(int(success))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn = self._connect()
        try:
            run = self._raw_query(conn, params)
            sql = f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?"
            rows = run(sql, (limit,)).fetchall()
        finally:
            conn.close()
        return [self._row_to_dict(row) for row in rows]

    def stats(
        self,
        path: str | None = None,
        service: str | None = None,
        success: bool | None = None,
    ) -> dict[str, Any]:
        """Aggregates for the dashboard (audit_log/dashboard.html) — computed here in
        SQL rather than by shipping every row for the client to tally, same
        "our own code does the deterministic work" principle as the agent's own
        sort/limit shaping. Accepts the same filters as query() so a filtered
        dashboard view and its underlying event table always agree.
        """
        clauses = []
        params: list[Any] = []
        if path is not None:
            clauses.append("path = ?")
            params.append(path)
        if service is not None:
            clauses.append("service = ?")
            params.append(service)
        if success is not None:
            clauses.append("success = ?")
            params.append(int(success))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        def _where(extra_clauses: list[str]) -> str:
            all_clauses = clauses + extra_clauses
            return f"WHERE {' AND '.join(all_clauses)}" if all_clauses else ""

        conn = self._connect()
        try:
            run = self._raw_query(conn, params)
            total_events = run(f"SELECT COUNT(*) FROM events {where}").fetchone()[0]
            total_questions = run(
                f"SELECT COUNT(DISTINCT question_id) FROM events {where}"
            ).fetchone()[0]
            success_sql = f"SELECT COUNT(*) FROM events {_where(['success = 1'])}"
            success_count = run(success_sql).fetchone()[0]
            by_path = dict(run(f"SELECT path, COUNT(*) FROM events {where} GROUP BY path"))
            by_service = dict(
                run(
                    f"SELECT service, COUNT(*) FROM events {_where(['service IS NOT NULL'])} "
                    "GROUP BY service"
                )
            )
            avg_duration = (
                run(f"SELECT AVG(duration_seconds) FROM events {where}").fetchone()[0] or 0.0
            )
        finally:
            conn.close()
        return {
            "total_events": total_events,
            "total_questions": total_questions,
            "success_count": success_count,
            "failure_count": total_events - success_count,
            "by_path": by_path,
            "by_service": by_service,
            "avg_duration_seconds": round(avg_duration, 3),
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["success"] = bool(data["success"])
        if data.get("params") is not None:
            data["params"] = json.loads(data["params"])
        return data
