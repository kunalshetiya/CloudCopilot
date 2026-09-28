"""Checks whether an AWS action is actually granted by the role this agent is
currently running as — distinct from ``iam/policy.json``'s aspirational,
hand-authored ask (docs/context.md section 8). ``iam/granted-actions.json`` is
a hand-maintained snapshot of what's really attached to the role in use right
now; kept in sync by hand whenever that role changes, same review discipline
as any other security artifact.

This exists because the role actually provisioned for this agent grants a
different set of actions than the one our registry/dispatch code was designed
against — narrower in places (e.g. Cost Explorer is entirely unreachable),
wider in others. Rather than let a real AWS call fail with ``AccessDenied``
and translate that after the fact, a matched capability is checked against
this file first, so the agent can decline clearly and specifically before
ever attempting the call.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

GRANTED_ACTIONS_PATH = Path(__file__).resolve().parent.parent / "iam" / "granted-actions.json"


def _load_granted_actions() -> frozenset[str]:
    data = json.loads(GRANTED_ACTIONS_PATH.read_text())
    return frozenset(data["actions"])


GRANTED_ACTIONS: frozenset[str] = _load_granted_actions()


def missing_actions(required: Iterable[str], granted: frozenset[str] | None = None) -> set[str]:
    """Which of ``required`` IAM actions aren't in ``granted``.

    ``granted`` defaults to the real role's actual grant (``GRANTED_ACTIONS``).
    Tests that want to exercise a capability's own behavior — independent of
    today's deployment constraints — pass their own ``granted`` set instead,
    the same dependency-injection seam used for ``session``/``openai_client``
    elsewhere in this project.
    """
    granted = GRANTED_ACTIONS if granted is None else granted
    return {action for action in required if action not in granted}
