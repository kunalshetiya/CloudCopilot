"""Unit tests for audit_log/storage.py: the SQLite-backed event store."""

from audit_log.storage import EventStore

BASE_EVENT = {
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


def test_insert_and_query_round_trips_all_fields(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")

    event_id = store.insert(BASE_EVENT)
    assert event_id == 1

    [row] = store.query()
    assert row["question"] == BASE_EVENT["question"]
    assert row["service"] == "ec2"
    assert row["params"] == {}
    assert row["success"] is True
    assert row["result_count"] == 2
    assert row["preview"] == BASE_EVENT["preview"]


def test_suggestion_event_has_null_aws_fields(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    suggestion_event = {
        **BASE_EVENT,
        "path": "suggestion",
        "service": None,
        "operation": None,
        "params": None,
        "result_count": None,
        "preview": None,
    }

    store.insert(suggestion_event)

    [row] = store.query()
    assert row["path"] == "suggestion"
    assert row["service"] is None
    assert row["params"] is None


def test_query_filters_by_question_id(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "question_id": "q-1"})
    store.insert({**BASE_EVENT, "question_id": "q-2"})

    results = store.query(question_id="q-2")

    assert len(results) == 1
    assert results[0]["question_id"] == "q-2"


def test_query_filters_by_success(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "success": True})
    store.insert({**BASE_EVENT, "success": False})

    failed = store.query(success=False)

    assert len(failed) == 1
    assert failed[0]["success"] is False


def test_query_filters_by_path(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "path": "registry"})
    store.insert({**BASE_EVENT, "path": "suggestion", "service": None})

    results = store.query(path="suggestion")

    assert len(results) == 1
    assert results[0]["path"] == "suggestion"


def test_query_filters_by_service(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "service": "ec2"})
    store.insert({**BASE_EVENT, "service": "s3"})

    results = store.query(service="s3")

    assert len(results) == 1
    assert results[0]["service"] == "s3"


def test_query_orders_most_recent_first(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "question_id": "first"})
    store.insert({**BASE_EVENT, "question_id": "second"})

    results = store.query()

    assert [r["question_id"] for r in results] == ["second", "first"]


def test_query_respects_limit(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    for i in range(5):
        store.insert({**BASE_EVENT, "question_id": f"q-{i}"})

    results = store.query(limit=2)

    assert len(results) == 2


def test_stats_on_an_empty_store(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")

    result = store.stats()

    assert result == {
        "total_events": 0,
        "total_questions": 0,
        "success_count": 0,
        "failure_count": 0,
        "by_path": {},
        "by_service": {},
        "avg_duration_seconds": 0.0,
    }


def test_stats_counts_events_and_distinct_questions(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    # One question ("q-1") triggers two real AWS calls — the same "one row per
    # call, not per question" shape the agent itself produces (context.md 7.5).
    store.insert({**BASE_EVENT, "question_id": "q-1", "service": "ec2"})
    store.insert({**BASE_EVENT, "question_id": "q-1", "service": "s3"})
    store.insert({**BASE_EVENT, "question_id": "q-2", "service": "ec2"})

    result = store.stats()

    assert result["total_events"] == 3
    assert result["total_questions"] == 2
    assert result["by_service"] == {"ec2": 2, "s3": 1}


def test_stats_breaks_down_success_and_failure(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "success": True})
    store.insert({**BASE_EVENT, "success": True})
    store.insert({**BASE_EVENT, "success": False})

    result = store.stats()

    assert result["success_count"] == 2
    assert result["failure_count"] == 1


def test_stats_breaks_down_by_path(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "path": "registry"})
    store.insert({**BASE_EVENT, "path": "fallback", "service": "rds"})
    store.insert({**BASE_EVENT, "path": "suggestion", "service": None})

    result = store.stats()

    assert result["by_path"] == {"registry": 1, "fallback": 1, "suggestion": 1}


def test_stats_null_service_events_are_not_double_counted(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "path": "suggestion", "service": None})

    result = store.stats()

    assert result["by_service"] == {}
    assert result["total_events"] == 1


def test_stats_respects_the_same_filters_as_query(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "path": "registry", "service": "ec2"})
    store.insert({**BASE_EVENT, "path": "fallback", "service": "s3"})

    result = store.stats(path="registry")

    assert result["total_events"] == 1
    assert result["by_service"] == {"ec2": 1}


def test_stats_filters_by_service(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "service": "ec2"})
    store.insert({**BASE_EVENT, "service": "s3"})

    result = store.stats(service="s3")

    assert result["total_events"] == 1
    assert result["by_service"] == {"s3": 1}


def test_stats_filters_by_success(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "success": True})
    store.insert({**BASE_EVENT, "success": False})

    result = store.stats(success=False)

    assert result["total_events"] == 1
    assert result["failure_count"] == 1


def test_stats_averages_duration(tmp_path):
    store = EventStore(db_path=tmp_path / "audit.db")
    store.insert({**BASE_EVENT, "duration_seconds": 0.1})
    store.insert({**BASE_EVENT, "duration_seconds": 0.3})

    result = store.stats()

    assert result["avg_duration_seconds"] == 0.2
