"""Unit tests for agent/style.py: terminal styling for the CLI's output.

A real test run never has a TTY attached (pytest's capsys/StringIO streams
both report isatty() as False), so these tests use a small fake stream to
exercise the color-enabled path deterministically, without needing an actual
terminal.
"""

from agent import style


class _FakeTty:
    def isatty(self):
        return True


class _FakeNonTty:
    def isatty(self):
        return False


def test_format_answer_is_unchanged_without_a_real_terminal():
    answer = "3 EC2 instance(s) currently running:\n- i-1: 2h\n- i-2: 3h"

    assert style.format_answer(answer, stream=_FakeNonTty()) == answer


def test_format_answer_bolds_the_headline_on_a_real_terminal():
    result = style.format_answer("3 EC2 instance(s) currently running:", stream=_FakeTty())

    assert result.startswith(style.BOLD)
    assert result.endswith(style.RESET)
    assert "3 EC2 instance(s) currently running:" in result


def test_format_answer_restyles_bullet_lines_on_a_real_terminal():
    answer = "2 security group(s) found:\n- sg-1: open\n- sg-2: open"

    result = style.format_answer(answer, stream=_FakeTty())

    lines = result.split("\n")
    assert style.BULLET in lines[1]
    assert "sg-1: open" in lines[1]
    assert style.BULLET in lines[2]
    assert "sg-2: open" in lines[2]


def test_format_answer_leaves_plain_continuation_lines_untouched():
    # e.g. a permission-gap decline followed by a suggestion, on its own line.
    answer = "I can't answer that — it needs `ce:GetCostAndUsage`.\n\nCheck the console instead."

    result = style.format_answer(answer, stream=_FakeTty())

    assert "Check the console instead." in result
    assert style.BULLET not in result.split("\n")[-1]


def test_format_answer_handles_empty_string():
    assert style.format_answer("", stream=_FakeTty()) == ""


def test_format_answer_respects_no_color_env_var(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    result = style.format_answer("headline", stream=_FakeTty())

    assert result == "headline"


def test_format_answer_respects_no_color_even_when_set_to_empty_string(monkeypatch):
    # The NO_COLOR convention (https://no-color.org) triggers on presence,
    # not truthiness — an empty value still means "disable."
    monkeypatch.setenv("NO_COLOR", "")

    result = style.format_answer("headline", stream=_FakeTty())

    assert result == "headline"


def test_prompt_is_plain_without_a_real_terminal():
    assert style.prompt(stream=_FakeNonTty()) == "> "


def test_prompt_is_styled_on_a_real_terminal():
    result = style.prompt(stream=_FakeTty())

    assert "❯" in result
    assert result != "> "


def test_banner_is_plain_without_a_real_terminal():
    assert style.banner("hello", stream=_FakeNonTty()) == "hello"


def test_banner_is_styled_on_a_real_terminal():
    result = style.banner("hello", stream=_FakeTty())

    assert style.BOLD in result
    assert "hello" in result
