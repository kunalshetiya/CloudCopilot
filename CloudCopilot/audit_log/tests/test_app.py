"""Unit tests for audit_log/app.py: the FastAPI audit-log service.

Uses FastAPI's TestClient (in-process, no real network or server) — the
module-level ``store`` is swapped for a fresh, temp-file-backed one per test
so tests never touch the service's real default database.
"""

import pytest
from fastapi.testclient import TestClient

import audit_log.app as app_module
from audit_log.storage import EventStore

EVENT = {
    "question": "which ec2 instances are running?",
    "question_id": "q-1",
    "path": "registry",
    "timestamp": "2026-08-04T00:00:00+00:00",
    "duration_seconds": 0.1,
    "success": True,
    "service": "ec2",
    "operation": "describe_instances",
    "params": {},
    "result_count": 2,
    "preview": "{'instances': []}",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "store", EventStore(db_path=tmp_path / "audit.db"))
    return TestClient(app_module.app)


def test_health_check(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_post_event_then_get_it_back(client):
    post_response = client.post("/events", json=EVENT)

    assert post_response.status_code == 201
    assert "id" in post_response.json()

    get_response = client.get("/events")

    assert get_response.status_code == 200
    [event] = get_response.json()
    assert event["question"] == EVENT["question"]
    assert event["service"] == "ec2"


def test_post_suggestion_event_with_null_fields(client):
    suggestion_event = {
        **EVENT,
        "path": "suggestion",
        "service": None,
        "operation": None,
        "params": None,
        "result_count": None,
        "preview": None,
    }

    response = client.post("/events", json=suggestion_event)

    assert response.status_code == 201
    [event] = client.get("/events").json()
    assert event["path"] == "suggestion"
    assert event["service"] is None


def test_get_events_filters_by_query_params(client):
    client.post("/events", json={**EVENT, "question_id": "q-1", "success": True})
    client.post("/events", json={**EVENT, "question_id": "q-2", "success": False})

    response = client.get("/events", params={"success": False})

    [event] = response.json()
    assert event["question_id"] == "q-2"


def test_get_stats_on_an_empty_service(client):
    response = client.get("/stats")

    assert response.status_code == 200
    assert response.json()["total_events"] == 0


def test_get_stats_reflects_posted_events(client):
    client.post("/events", json={**EVENT, "question_id": "q-1", "service": "ec2"})
    client.post("/events", json={**EVENT, "question_id": "q-1", "service": "s3"})
    client.post("/events", json={**EVENT, "question_id": "q-2", "service": "iam", "success": False})

    response = client.get("/stats")

    body = response.json()
    assert body["total_events"] == 3
    assert body["total_questions"] == 2
    assert body["success_count"] == 2
    assert body["failure_count"] == 1
    assert body["by_service"] == {"ec2": 1, "s3": 1, "iam": 1}


def test_get_stats_accepts_the_same_filters_as_events(client):
    client.post("/events", json={**EVENT, "path": "registry"})
    client.post("/events", json={**EVENT, "path": "fallback", "service": "s3"})

    response = client.get("/stats", params={"path": "fallback"})

    assert response.json()["total_events"] == 1
    assert response.json()["by_service"] == {"s3": 1}


def test_get_dashboard_serves_html(client):
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "CloudCopilot Audit Trail" in response.text
