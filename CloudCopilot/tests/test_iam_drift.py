"""Drift-check: the hand-authored IAM policy must match the registry and the
dynamic-dispatch allow-list exactly — no more, no less (docs/context.md 7.4).
"""

import json
from pathlib import Path

from agent.dispatch import ALLOWLIST as DISPATCH_ALLOWLIST
from agent.registry import REGISTRY

POLICY_PATH = Path(__file__).resolve().parent.parent / "iam" / "policy.json"


def _policy_actions() -> set[str]:
    policy = json.loads(POLICY_PATH.read_text())
    actions: set[str] = set()
    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow", "drift-check only understands Allow statements"
        action = statement["Action"]
        actions.update([action] if isinstance(action, str) else action)
    return actions


def _expected_actions() -> set[str]:
    registry_actions = {call.iam_action for entry in REGISTRY for call in entry.aws_calls}
    dispatch_actions = {call.iam_action for call in DISPATCH_ALLOWLIST}
    return registry_actions | dispatch_actions


def test_policy_grants_every_expected_action():
    missing = _expected_actions() - _policy_actions()
    assert not missing, f"registry/dispatch use actions the policy doesn't grant: {missing}"


def test_policy_grants_nothing_beyond_the_registry_and_dispatch_allowlist():
    extra = _policy_actions() - _expected_actions()
    assert not extra, f"policy grants actions nothing actually uses: {extra}"
