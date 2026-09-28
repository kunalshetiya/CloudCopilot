"""Unit tests for agent/dispatch.py: structured dynamic dispatch.

Mirrors test_registry.py's approach: moto for real AWS services that track
create/list state, verifying the allow-list/deny-list enforcement, pagination
with a cap, and audit behavior for both successful and blocked calls.
"""

import time

import boto3
import pytest
from botocore.exceptions import ParamValidationError
from moto import mock_aws

from agent.dispatch import (
    ALLOWLIST,
    DENYLIST,
    DispatchNotAllowed,
    _find_list_field,
    _paginated_with_cap,
    find_call,
    is_allowed,
    run_curated_call,
)


def test_allowlist_and_denylist_do_not_overlap():
    allowed_pairs = {(call.service, call.operation) for call in ALLOWLIST}
    denied_pairs = set(DENYLIST)
    assert allowed_pairs.isdisjoint(denied_pairs)


def test_is_allowed_true_for_allowlisted_operation():
    assert is_allowed("ec2", "describe_volumes") is True


def test_is_allowed_false_for_denylisted_operation():
    assert is_allowed("s3", "get_object") is False


def test_is_allowed_false_for_unlisted_operation():
    assert is_allowed("dynamodb", "list_tables") is False


def test_run_curated_call_rejects_denylisted_operation_and_audits_it():
    events = []

    with pytest.raises(DispatchNotAllowed) as exc_info:
        run_curated_call("ssm", "get_parameter", {"Name": "x"}, audit=events.append)

    assert len(events) == 1
    assert events[0].success is False
    assert events[0].service == "ssm"
    assert events[0].operation == "get_parameter"
    assert events[0].preview == DENYLIST[("ssm", "get_parameter")]
    assert exc_info.value.reason == DENYLIST[("ssm", "get_parameter")]


def test_run_curated_call_rejects_unlisted_operation_without_making_any_aws_call():
    events = []
    # dynamodb.list_tables is neither allow- nor deny-listed — genuinely
    # unreviewed, unlike dynamodb.scan (which is on DENYLIST, tested above).

    with pytest.raises(DispatchNotAllowed) as exc_info:
        run_curated_call("dynamodb", "list_tables", {}, audit=events.append)

    assert exc_info.value.reason is None

    assert events[0].preview == "not on the curated allow-list"


@mock_aws
def test_run_curated_call_runs_a_real_allowlisted_operation(session):
    ec2 = session.client("ec2")
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=10)
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)
    events = []

    result = run_curated_call("ec2", "describe_volumes", session=session, audit=events.append)

    assert len(result["Volumes"]) == 2
    assert result["_truncated"] is False
    assert len(events) == 1
    assert events[0].success is True
    assert events[0].result_count == 2


@mock_aws
def test_run_curated_call_runs_ecr_describe_repositories(session):
    ecr = session.client("ecr")
    ecr.create_repository(repositoryName="cloudcopilot-agent")
    events = []

    result = run_curated_call("ecr", "describe_repositories", session=session, audit=events.append)

    assert len(result["repositories"]) == 1
    assert result["repositories"][0]["repositoryName"] == "cloudcopilot-agent"
    assert events[0].success is True
    assert events[0].result_count == 1


@mock_aws
def test_run_curated_call_runs_ec2_vpc_networking_describe_calls(session):
    ec2 = session.client("ec2")
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet_id = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]

    subnets = run_curated_call("ec2", "describe_subnets", session=session)
    route_tables = run_curated_call("ec2", "describe_route_tables", session=session)
    igws = run_curated_call("ec2", "describe_internet_gateways", session=session)
    nat_gws = run_curated_call("ec2", "describe_nat_gateways", session=session)
    nacls = run_curated_call("ec2", "describe_network_acls", session=session)

    assert subnet_id in {s["SubnetId"] for s in subnets["Subnets"]}
    assert route_tables["RouteTables"]  # default VPC route table always exists
    assert igws["InternetGateways"] == []
    assert nat_gws["NatGateways"] == []
    assert nacls["NetworkAcls"]  # default network ACL always exists


@mock_aws
def test_run_curated_call_runs_s3_get_bucket_policy_status(session):
    s3 = session.client("s3")
    s3.create_bucket(Bucket="my-bucket")
    s3.put_bucket_policy(
        Bucket="my-bucket",
        Policy=(
            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*",'
            '"Action":"s3:GetObject","Resource":"arn:aws:s3:::my-bucket/*"}]}'
        ),
    )

    result = run_curated_call(
        "s3", "get_bucket_policy_status", {"Bucket": "my-bucket"}, session=session
    )

    assert "PolicyStatus" in result


@mock_aws
def test_run_curated_call_paginates_and_merges_pages(session):
    cloudwatch = session.client("cloudwatch")
    for i in range(3):
        cloudwatch.put_metric_data(
            Namespace="TestNamespace",
            MetricData=[{"MetricName": f"metric-{i}", "Value": 1.0}],
        )

    result = run_curated_call(
        "cloudwatch", "list_metrics", {"Namespace": "TestNamespace"}, session=session
    )

    assert len(result["Metrics"]) == 3


@mock_aws
def test_run_curated_call_caps_items_and_reports_truncation(session, monkeypatch):
    import agent.dispatch as dispatch_module

    monkeypatch.setattr(dispatch_module, "MAX_ITEMS", 1)
    ec2 = session.client("ec2")
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=10)
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)

    result = run_curated_call("ec2", "describe_volumes", session=session)

    assert len(result["Volumes"]) == 1
    assert result["_truncated"] is True


def test_run_curated_call_reports_failure_and_reraises(session):
    events = []
    with pytest.raises(ParamValidationError):
        run_curated_call(
            "ec2", "describe_regions", {"BogusParam": 1}, session=session, audit=events.append
        )
    assert events[0].success is False
    assert events[0].preview == "ParamValidationError"


def test_find_list_field_returns_none_when_no_list_present():
    assert _find_list_field({"foo": "bar"}) is None


def test_find_call_returns_matching_allowlist_entry():
    call = find_call("ec2", "describe_volumes")
    assert call is not None
    assert call.iam_action == "ec2:DescribeVolumes"


def test_find_call_returns_none_for_unlisted_operation():
    assert find_call("dynamodb", "scan") is None


@mock_aws
def test_run_curated_call_flags_suspicious_looking_s3_keys(session):
    s3 = session.client("s3")
    s3.create_bucket(Bucket="my-bucket")
    s3.put_object(Bucket="my-bucket", Key="passwords-backup.txt", Body=b"x")
    s3.put_object(Bucket="my-bucket", Key="reports/2026-08.csv", Body=b"x")

    result = run_curated_call("s3", "list_objects_v2", {"Bucket": "my-bucket"}, session=session)

    flagged = {item["Key"]: item["_flagged"] for item in result["Contents"]}
    assert flagged["passwords-backup.txt"] is True
    assert flagged["reports/2026-08.csv"] is False


@mock_aws
def test_run_curated_call_strips_log_events_to_a_bare_count(session):
    logs = session.client("logs")
    logs.create_log_group(logGroupName="my-group")
    logs.create_log_stream(logGroupName="my-group", logStreamName="my-stream")
    logs.put_log_events(
        logGroupName="my-group",
        logStreamName="my-stream",
        logEvents=[
            {
                "timestamp": int(time.time() * 1000),
                "message": "super-secret-password=hunter2",
            }
        ],
    )

    result = run_curated_call(
        "logs", "filter_log_events", {"logGroupName": "my-group"}, session=session
    )

    assert result == {"event_count": 1, "_truncated": False}
    assert "hunter2" not in str(result)


class _EmptyPaginator:
    def paginate(self, **params):
        return iter([])


class _NoPaginationClient:
    def get_paginator(self, operation):
        return _EmptyPaginator()


def test_paginated_with_cap_returns_empty_result_when_there_are_no_pages_at_all():
    result, truncated = _paginated_with_cap(_NoPaginationClient(), "describe_things", {})
    assert result == {}
    assert truncated is False


@pytest.fixture
def session():
    return boto3.Session(region_name="us-east-1")
