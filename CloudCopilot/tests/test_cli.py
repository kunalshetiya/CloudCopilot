"""Unit tests for agent/cli.py: single-question and interactive CLI modes.

``answer_question``/``run_single_question``/``run_interactive`` are monkeypatched
here rather than exercised for real — routing correctness is covered in
test_router.py. These tests are only about the CLI's own plumbing: argument
dispatch, the interactive loop's exit/blank-line handling, and output.
"""

import io

import agent.cli as cli
from agent.audit import FileAuditLog, HttpAuditLog


class _StubAuditLog:
    def record(self, event):
        pass


def test_default_audit_log_is_file_based_without_audit_log_url(monkeypatch):
    monkeypatch.delenv("AUDIT_LOG_URL", raising=False)

    assert isinstance(cli._default_audit_log(), FileAuditLog)


def test_default_audit_log_warns_on_stderr_about_the_dashboard_gap(monkeypatch, capsys):
    monkeypatch.delenv("AUDIT_LOG_URL", raising=False)

    cli._default_audit_log()

    assert "won't appear on the audit dashboard" in capsys.readouterr().err


def test_default_audit_log_is_http_based_when_audit_log_url_is_set(monkeypatch, capsys):
    monkeypatch.setenv("AUDIT_LOG_URL", "http://audit-log:8001")

    log = cli._default_audit_log()

    assert isinstance(log, HttpAuditLog)
    assert log.base_url == "http://audit-log:8001"
    assert capsys.readouterr().err == ""


def test_run_single_question_delegates_to_answer_question(monkeypatch):
    seen = {}

    def fake_answer_question(question, *, audit_log):
        seen["question"] = question
        return "the answer"

    monkeypatch.setattr(cli, "answer_question", fake_answer_question)

    result = cli.run_single_question("how many ec2 instances?", audit_log=_StubAuditLog())

    assert result == "the answer"
    assert seen["question"] == "how many ec2 instances?"


def test_run_interactive_answers_each_question_and_stops_on_exit(monkeypatch):
    monkeypatch.setattr(cli, "answer_question", lambda q, *, audit_log: f"answer: {q}")
    out = io.StringIO()

    cli.run_interactive(
        ["first question", "second question", "exit", "never reached"],
        out,
        audit_log=_StubAuditLog(),
    )

    output = out.getvalue()
    assert "answer: first question" in output
    assert "answer: second question" in output
    assert "never reached" not in output


def test_run_interactive_skips_blank_lines(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "answer_question", lambda q, *, audit_log: calls.append(q) or "ok")
    out = io.StringIO()

    cli.run_interactive(["", "   ", "a real question", "exit"], out, audit_log=_StubAuditLog())

    assert calls == ["a real question"]


def test_main_single_question_mode(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_single_question", lambda q, **kwargs: f"answered: {q}")

    exit_code = cli.main(["what", "instances", "are", "running?"])

    assert exit_code == 0
    assert "answered: what instances are running?" in capsys.readouterr().out


def test_main_interactive_mode_with_no_args(monkeypatch, capsys):
    called = {}
    monkeypatch.setattr(
        cli, "run_interactive", lambda lines, **kwargs: called.setdefault("ran", True)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("exit\n"))

    exit_code = cli.main([])

    assert exit_code == 0
    assert called.get("ran")
    assert "interactive mode" in capsys.readouterr().out.lower()
