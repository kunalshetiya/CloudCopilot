"""Unit tests for agent/permissions.py: the permission gate that checks a
capability's required IAM actions against the role actually deployed
(docs/context.md section 8), separately from iam/policy.json's aspirational
drift-check in test_iam_drift.py.
"""

from agent import dispatch, permissions
from agent.registry import REGISTRY


def test_missing_actions_returns_empty_when_all_granted():
    granted = frozenset({"ec2:DescribeInstances", "s3:ListAllMyBuckets"})
    assert permissions.missing_actions(["ec2:DescribeInstances"], granted=granted) == set()


def test_missing_actions_returns_ungranted_subset():
    granted = frozenset({"ec2:DescribeInstances"})
    result = permissions.missing_actions(
        ["ec2:DescribeInstances", "ce:GetCostAndUsage"], granted=granted
    )
    assert result == {"ce:GetCostAndUsage"}


def test_missing_actions_defaults_to_the_real_granted_role():
    # ec2:DescribeInstances really is granted today; ce:GetCostAndUsage really
    # isn't (docs/context.md section 8) — no override passed here on purpose.
    assert permissions.missing_actions(["ec2:DescribeInstances"]) == set()
    assert permissions.missing_actions(["ce:GetCostAndUsage"]) == {"ce:GetCostAndUsage"}


def test_known_permission_gap_matches_what_context_md_documents():
    """Locks in exactly which of our own registry/dispatch actions the
    currently-deployed role doesn't cover. This is expected to fail — on
    purpose — the moment either the real role's grant changes or our code's
    own action set changes, forcing a conscious update rather than a silent
    drift between what context.md claims and what's actually true."""
    registry_actions = {call.iam_action for entry in REGISTRY for call in entry.aws_calls}
    dispatch_actions = {call.iam_action for call in dispatch.ALLOWLIST}
    all_actions = registry_actions | dispatch_actions

    assert permissions.missing_actions(all_actions) == {
        "s3:ListAllMyBuckets",
        "s3:GetBucketPublicAccessBlock",
        "iam:ListRoles",
        "iam:ListUsers",
        "iam:ListGroups",
        "ce:GetCostAndUsage",
        "cloudwatch:ListMetrics",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:GetMetricData",
        "ec2:DescribeVolumes",
        "ec2:DescribeSnapshots",
        "ec2:DescribeAddresses",
        "ec2:DescribeRegions",
        "rds:DescribeDBSnapshots",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth",
        "autoscaling:DescribeAutoScalingGroups",
        "logs:FilterLogEvents",
        "logs:DescribeLogGroups",
        "ecr:DescribeRepositories",
    }
