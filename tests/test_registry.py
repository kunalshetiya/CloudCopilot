"""Unit tests for the fixed capability registry (agent/registry.py).

Four of the five capabilities are tested against moto's simulated AWS backend,
since moto tracks real create/list state for EC2, S3, and IAM. Cost Explorer is
tested with botocore's Stubber instead — moto doesn't simulate real billing
data (see docs/learning-notes.md), so there's nothing for a mocked account to
track for that one service.
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from moto import mock_aws

from agent.registry import (
    _failure_preview,
    _paginated,
    audited_call,
    cost_by_service_this_month,
    ec2_instances,
    iam_roles_users_and_groups,
    rds_clusters,
    rds_instances,
    s3_public_access,
    security_groups_open_to_internet,
)


@pytest.fixture
def session():
    return boto3.Session(region_name="us-east-1")


class _StubSession:
    """A minimal stand-in for boto3.Session that hands back one pre-built client."""

    def __init__(self, client):
        self._client = client

    def client(self, service_name):
        return self._client


# ---------------------------------------------------------------------------
# _paginated — the shared helper four of the five capabilities rely on
# ---------------------------------------------------------------------------


def test_paginated_merges_multiple_pages():
    client = boto3.Session(region_name="us-east-1").client("ec2")
    stubber = Stubber(client)
    stubber.add_response(
        "describe_instances",
        {
            "Reservations": [{"ReservationId": "r-1", "Instances": [{"InstanceId": "i-1"}]}],
            "NextToken": "page-2",
        },
    )
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"ReservationId": "r-2", "Instances": [{"InstanceId": "i-2"}]}]},
    )
    stubber.activate()

    result = _paginated(client, "describe_instances")

    assert [r["ReservationId"] for r in result["Reservations"]] == ["r-1", "r-2"]
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# ec2_instances
# ---------------------------------------------------------------------------


@mock_aws
def test_ec2_instances_lists_running_instances(session):
    ec2 = session.client("ec2")
    ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")

    result = ec2_instances(session=session)

    assert len(result["instances"]) == 1
    instance = result["instances"][0]
    assert instance["instance_id"].startswith("i-")
    assert instance["uptime_hours"] >= 0
    assert instance["state"] == "running"


@mock_aws
def test_ec2_instances_excludes_stopped_instances_by_default(session):
    ec2 = session.client("ec2")
    run = ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")
    instance_id = run["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[instance_id])

    result = ec2_instances(session=session)

    assert result["instances"] == []


@mock_aws
def test_ec2_instances_state_stopped_returns_only_stopped(session):
    ec2 = session.client("ec2")
    ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")
    run2 = ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")
    ec2.stop_instances(InstanceIds=[run2["Instances"][0]["InstanceId"]])

    result = ec2_instances(session=session, state="stopped")

    assert len(result["instances"]) == 1
    assert result["instances"][0]["state"] == "stopped"
    assert result["instances"][0]["uptime_hours"] is None


@mock_aws
def test_ec2_instances_state_all_returns_every_state(session):
    ec2 = session.client("ec2")
    ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")
    run2 = ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")
    ec2.stop_instances(InstanceIds=[run2["Instances"][0]["InstanceId"]])

    result = ec2_instances(session=session, state="all")

    assert len(result["instances"]) == 2
    assert result["state_filter"] == "all"


@mock_aws
def test_ec2_instances_with_no_threshold_reports_none(session):
    result = ec2_instances(session=session)

    assert result["min_uptime_hours"] is None


@mock_aws
def test_ec2_instances_min_uptime_hours_excludes_instances_below_threshold(session):
    ec2 = session.client("ec2")
    ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")

    # A freshly-launched instance has ~0h uptime, so a 24h threshold excludes it.
    result = ec2_instances(session=session, min_uptime_hours=24)

    assert result["instances"] == []
    assert result["min_uptime_hours"] == 24.0


@mock_aws
def test_ec2_instances_min_uptime_hours_includes_instances_at_or_above_threshold(session):
    ec2 = session.client("ec2")
    ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")

    # 0h threshold: every running instance's uptime is always >= 0.
    result = ec2_instances(session=session, min_uptime_hours=0)

    assert len(result["instances"]) == 1


@mock_aws
def test_ec2_instances_min_uptime_hours_ignored_for_non_running_states(session):
    # A threshold only makes sense for running instances — a stopped one has
    # no uptime to compare, so it's excluded regardless of the threshold value.
    ec2 = session.client("ec2")
    run = ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")
    ec2.stop_instances(InstanceIds=[run["Instances"][0]["InstanceId"]])

    result = ec2_instances(session=session, state="stopped", min_uptime_hours=0)

    assert result["instances"] == []


@mock_aws
def test_ec2_instances_reports_rich_attributes(session):
    ec2 = session.client("ec2")
    run = ec2.run_instances(
        ImageId="ami-1",
        MinCount=1,
        MaxCount=1,
        InstanceType="t2.micro",
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "my-instance"}]}
        ],
    )
    instance_id = run["Instances"][0]["InstanceId"]

    result = ec2_instances(session=session)

    [instance] = result["instances"]
    assert instance["instance_id"] == instance_id
    assert instance["instance_type"] == "t2.micro"
    assert instance["ami_id"] == "ami-1"
    assert instance["name_tag"] == "my-instance"
    assert instance["availability_zone"]
    assert instance["vpc_id"]


@mock_aws
def test_ec2_instances_reports_missing_name_tag(session):
    ec2 = session.client("ec2")
    ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")

    result = ec2_instances(session=session)

    assert result["instances"][0]["name_tag"] is None


# ---------------------------------------------------------------------------
# rds_instances / rds_clusters
# ---------------------------------------------------------------------------


@mock_aws
def test_rds_instances_lists_instances_with_details(session):
    rds = session.client("rds")
    rds.create_db_instance(
        DBInstanceIdentifier="my-db",
        Engine="postgres",
        DBInstanceClass="db.t3.micro",
        AllocatedStorage=20,
        StorageEncrypted=True,
        MasterUsername="admin",
        MasterUserPassword="hunter22",
    )

    result = rds_instances(session=session)

    [instance] = result["instances"]
    assert instance["db_instance_identifier"] == "my-db"
    assert instance["engine"] == "postgres"
    assert instance["instance_class"] == "db.t3.micro"
    assert instance["allocated_storage_gb"] == 20
    assert instance["storage_encrypted"] is True


@mock_aws
def test_rds_instances_empty_account_returns_no_instances(session):
    result = rds_instances(session=session)

    assert result["instances"] == []


@mock_aws
def test_rds_clusters_lists_clusters_with_details(session):
    rds = session.client("rds")
    rds.create_db_cluster(
        DBClusterIdentifier="my-cluster",
        Engine="aurora-postgresql",
        MasterUsername="admin",
        MasterUserPassword="hunter22",
        StorageEncrypted=True,
    )

    result = rds_clusters(session=session)

    [cluster] = result["clusters"]
    assert cluster["db_cluster_identifier"] == "my-cluster"
    assert cluster["engine"] == "aurora-postgresql"
    assert cluster["storage_encrypted"] is True


@mock_aws
def test_rds_clusters_empty_account_returns_no_clusters(session):
    result = rds_clusters(session=session)

    assert result["clusters"] == []


# ---------------------------------------------------------------------------
# security_groups_open_to_internet
# ---------------------------------------------------------------------------


@mock_aws
def test_flags_security_group_open_to_internet(session):
    ec2 = session.client("ec2")
    sg = ec2.create_security_group(GroupName="open-sg", Description="test")
    ec2.authorize_security_group_ingress(
        GroupId=sg["GroupId"],
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )

    result = security_groups_open_to_internet(session=session)

    flagged_ids = [g["group_id"] for g in result["flagged_groups"]]
    assert sg["GroupId"] in flagged_ids


@mock_aws
def test_does_not_flag_group_restricted_to_a_specific_ip(session):
    ec2 = session.client("ec2")
    sg = ec2.create_security_group(GroupName="closed-sg", Description="test")
    ec2.authorize_security_group_ingress(
        GroupId=sg["GroupId"],
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "10.0.0.5/32"}],
            }
        ],
    )

    result = security_groups_open_to_internet(session=session)

    flagged_ids = [g["group_id"] for g in result["flagged_groups"]]
    assert sg["GroupId"] not in flagged_ids


@mock_aws
def test_default_security_group_open_egress_is_not_flagged(session):
    # The default VPC security group has 0.0.0.0/0 *egress* open out of the
    # box, but no inbound rules. Only inbound (IpPermissions) makes a group
    # reachable from the internet, so this must not be flagged.
    result = security_groups_open_to_internet(session=session)

    assert result["flagged_groups"] == []


# ---------------------------------------------------------------------------
# s3_public_access
# ---------------------------------------------------------------------------


@mock_aws
def test_bucket_with_no_public_access_block_is_reported_as_not_blocked(session):
    s3 = session.client("s3")
    s3.create_bucket(Bucket="open-bucket")

    result = s3_public_access(session=session)

    bucket = next(b for b in result["buckets"] if b["bucket"] == "open-bucket")
    assert bucket["blocks_public_access"] is False


@mock_aws
def test_bucket_with_full_public_access_block_is_reported_as_blocked(session):
    s3 = session.client("s3")
    s3.create_bucket(Bucket="locked-bucket")
    s3.put_public_access_block(
        Bucket="locked-bucket",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    result = s3_public_access(session=session)

    bucket = next(b for b in result["buckets"] if b["bucket"] == "locked-bucket")
    assert bucket["blocks_public_access"] is True


@mock_aws
def test_bucket_with_partial_public_access_block_is_reported_as_not_blocked(session):
    s3 = session.client("s3")
    s3.create_bucket(Bucket="partial-bucket")
    s3.put_public_access_block(
        Bucket="partial-bucket",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    result = s3_public_access(session=session)

    bucket = next(b for b in result["buckets"] if b["bucket"] == "partial-bucket")
    assert bucket["blocks_public_access"] is False


# ---------------------------------------------------------------------------
# iam_roles_users_and_groups
# ---------------------------------------------------------------------------


@mock_aws
def test_lists_iam_roles_users_and_groups(session):
    iam = session.client("iam")
    iam.create_role(RoleName="my-role", AssumeRolePolicyDocument="{}")
    iam.create_user(UserName="my-user")
    iam.create_group(GroupName="my-group")

    result = iam_roles_users_and_groups(session=session)

    assert "my-role" in result["roles"]
    assert "my-user" in result["users"]
    assert "my-group" in result["groups"]


# ---------------------------------------------------------------------------
# cost_by_service_this_month
# ---------------------------------------------------------------------------


def _stub_ce_session(responses):
    client = boto3.Session(region_name="us-east-1").client("ce")
    stubber = Stubber(client)
    for response in responses:
        stubber.add_response("get_cost_and_usage", response)
    stubber.activate()
    return _StubSession(client), stubber


def test_cost_by_service_parses_single_page_breakdown():
    response = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-04"},
                "Total": {},
                "Groups": [
                    {
                        "Keys": ["Amazon EC2"],
                        "Metrics": {"UnblendedCost": {"Amount": "12.3456", "Unit": "USD"}},
                    },
                ],
                "Estimated": False,
            }
        ],
    }
    session, stubber = _stub_ce_session([response])

    result = cost_by_service_this_month(session=session)

    assert result["breakdown"] == [{"service": "Amazon EC2", "cost_usd": 12.35}]
    stubber.assert_no_pending_responses()


def test_cost_by_service_follows_next_page_token():
    page1 = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-04"},
                "Total": {},
                "Groups": [
                    {
                        "Keys": ["Amazon EC2"],
                        "Metrics": {"UnblendedCost": {"Amount": "12.3456", "Unit": "USD"}},
                    }
                ],
                "Estimated": False,
            }
        ],
        "NextPageToken": "page-2-token",
    }
    page2 = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-04"},
                "Total": {},
                "Groups": [
                    {
                        "Keys": ["Amazon S3"],
                        "Metrics": {"UnblendedCost": {"Amount": "1.005", "Unit": "USD"}},
                    }
                ],
                "Estimated": False,
            }
        ],
    }
    session, stubber = _stub_ce_session([page1, page2])

    result = cost_by_service_this_month(session=session)

    assert result["breakdown"] == [
        {"service": "Amazon EC2", "cost_usd": 12.35},
        {"service": "Amazon S3", "cost_usd": 1.0},
    ]
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# audited_call / audit threading — one audit event per real AWS call
# ---------------------------------------------------------------------------


def test_audited_call_reports_success_and_reraises_nothing():
    client = boto3.Session(region_name="us-east-1").client("ec2")
    stubber = Stubber(client)
    stubber.add_response("describe_regions", {"Regions": [{"RegionName": "us-east-1"}]})
    stubber.activate()
    events = []

    audited_call(client, "describe_regions", events.append)

    record = events[0]
    assert (record.service, record.operation, record.success, record.result_count) == (
        "ec2",
        "describe_regions",
        True,
        1,
    )
    assert record.duration_seconds >= 0
    assert record.preview is not None
    stubber.assert_no_pending_responses()


def test_audited_call_reports_failure_and_still_reraises():
    client = boto3.Session(region_name="us-east-1").client("ec2")
    stubber = Stubber(client)
    stubber.add_client_error("describe_regions", service_error_code="Throttling")
    stubber.activate()
    events = []

    with pytest.raises(ClientError):
        audited_call(client, "describe_regions", events.append)

    record = events[0]
    assert (record.service, record.operation, record.success, record.result_count) == (
        "ec2",
        "describe_regions",
        False,
        None,
    )
    assert record.preview == "Throttling"


def test_failure_preview_uses_the_aws_error_code_for_a_client_error():
    exc = ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DescribeRegions")

    assert _failure_preview(exc) == "AccessDenied"


def test_failure_preview_falls_back_to_the_exception_class_name_otherwise():
    assert _failure_preview(ValueError("some detail that stays out of the audit log")) == (
        "ValueError"
    )


@mock_aws
def test_ec2_instances_reports_one_audit_event_for_the_paginated_call(session):
    ec2 = session.client("ec2")
    ec2.run_instances(ImageId="ami-1", MinCount=1, MaxCount=1, InstanceType="t2.micro")
    events = []

    ec2_instances(session=session, audit=events.append)

    assert len(events) == 1
    record = events[0]
    assert (record.service, record.operation, record.success, record.result_count) == (
        "ec2",
        "describe_instances",
        True,
        1,
    )


@mock_aws
def test_s3_public_access_reports_one_call_per_bucket_plus_one_list_call(session):
    s3 = session.client("s3")
    s3.create_bucket(Bucket="bucket-one")
    s3.create_bucket(Bucket="bucket-two")
    events = []

    s3_public_access(session=session, audit=events.append)

    operations = [e.operation for e in events]
    assert operations.count("list_buckets") == 1
    assert operations.count("get_public_access_block") == 2


def test_response_count_falls_back_to_one_when_no_list_field_present():
    from agent.registry import _response_count

    assert _response_count({"SomeConfig": {"nested": True}}) == 1


def test_paginated_reports_failure_and_still_reraises():
    client = boto3.Session(region_name="us-east-1").client("ec2")
    stubber = Stubber(client)
    stubber.add_client_error("describe_instances", service_error_code="Throttling")
    stubber.activate()
    events = []

    with pytest.raises(ClientError):
        _paginated(client, "describe_instances", audit=events.append)

    assert events[0].success is False


def test_cost_by_service_reports_one_audit_event_on_success():
    response = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-04"},
                "Total": {},
                "Groups": [
                    {
                        "Keys": ["Amazon EC2"],
                        "Metrics": {"UnblendedCost": {"Amount": "5.00", "Unit": "USD"}},
                    }
                ],
                "Estimated": False,
            }
        ],
    }
    session, stubber = _stub_ce_session([response])
    events = []

    cost_by_service_this_month(session=session, audit=events.append)

    assert len(events) == 1
    record = events[0]
    assert (record.service, record.operation, record.success, record.result_count) == (
        "ce",
        "get_cost_and_usage",
        True,
        1,
    )


def test_cost_by_service_reports_failure_and_still_reraises():
    client = boto3.Session(region_name="us-east-1").client("ce")
    stubber = Stubber(client)
    stubber.add_client_error("get_cost_and_usage", service_error_code="ThrottlingException")
    stubber.activate()
    events = []

    with pytest.raises(ClientError):
        cost_by_service_this_month(session=_StubSession(client), audit=events.append)

    assert events[0].success is False
