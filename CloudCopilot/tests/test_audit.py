"""Unit tests for agent/audit.py: the audit-log client interface."""

import json

import httpx

from agent.audit import AuditEvent, FileAuditLog, HttpAuditLog


def test_file_audit_log_appends_one_json_line_per_event(tmp_path):
    path = tmp_path / "events.jsonl"
    log = FileAuditLog(path=path)

    log.record(
        AuditEvent(
            question="how many ec2 instances are running?",
            question_id="id-1",
            path="registry",
            timestamp="2026-08-04T00:00:00+00:00",
            duration_seconds=0.1,
            success=True,
            service="ec2",
            operation="describe_instances",
            params={},
            result_count=2,
        )
    )
    log.record(
        AuditEvent(
            question="what's my cpu usage?",
            question_id="id-2",
            path="suggestion",
            timestamp="2026-08-04T00:00:01+00:00",
            duration_seconds=0.2,
            success=True,
        )
    )

    lines = path.read_text().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["question_id"] == "id-1"
    assert first["service"] == "ec2"
    assert first["result_count"] == 2

    second = json.loads(lines[1])
    assert second["path"] == "suggestion"
    assert second["service"] is None
    assert second["operation"] is None


def test_file_audit_log_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "events.jsonl"
    log = FileAuditLog(path=path)

    log.record(
        AuditEvent(
            question="q",
            question_id="id",
            path="suggestion",
            timestamp="2026-08-04T00:00:00+00:00",
            duration_seconds=0.0,
            success=True,
        )
    )

    assert path.exists()


def _make_event(**overrides) -> AuditEvent:
    defaults = dict(
        question="which ec2 instances are running?",
        question_id="id-1",
        path="registry",
        timestamp="2026-08-04T00:00:00+00:00",
        duration_seconds=0.1,
        success=True,
        service="ec2",
        operation="describe_instances",
        params={},
        result_count=2,
        preview="{'instances': []}",
    )
    return AuditEvent(**{**defaults, **overrides})


def test_http_audit_log_posts_event_to_the_service():
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["url"] = str(request.url)
        received["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    log = HttpAuditLog(base_url="http://audit-log:8001", client=client)

    log.record(_make_event())

    assert received["url"] == "http://audit-log:8001/events"
    assert received["body"]["question_id"] == "id-1"


def test_http_audit_log_swallows_failures_without_raising(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    log = HttpAuditLog(client=client)

    log.record(_make_event())  # must not raise

    assert "failed to record audit event" in capsys.readouterr().err
