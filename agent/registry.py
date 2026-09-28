"""The fixed capability registry: AWS operations CloudCopilot can answer directly.

Each entry is self-describing (name, description, parameter schema, and the exact
AWS calls it makes), so the LLM's menu, the CLI's help text, and the IAM
drift-check test all read from this one list instead of five different places.
See docs/context.md section 7.2/7.3.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3


@dataclass(frozen=True)
class AwsCallRecord:
    """One real AWS call, reported for audit. Registry code only ever builds
    this bare, generic record and hands it to a callback — it has no idea what
    an "audit log" is or how one gets recorded. See agent/audit.py and
    agent/router.py for what actually happens on the other end."""

    service: str
    operation: str
    params: dict[str, Any]
    success: bool
    result_count: int | None
    preview: str | None
    duration_seconds: float


# Called once per real AWS call, if given.
AuditCallback = Callable[[AwsCallRecord], None]


@dataclass(frozen=True)
class AwsCall:
    """One AWS API call a registry entry is allowed to make.

    ``iam_action`` is written by hand, not derived from ``operation``: IAM action
    names don't always match the boto3 method name. For example, the ListBuckets
    API is authorized by ``s3:ListAllMyBuckets``, and the bucket-level
    GetPublicAccessBlock API is authorized by ``s3:GetBucketPublicAccessBlock`` —
    both differ from a mechanical snake_case-to-PascalCase conversion.
    """

    service: str  # boto3 client name, e.g. "ec2"
    operation: str  # exact boto3 client method name, e.g. "describe_instances"
    iam_action: str  # exact IAM action string, e.g. "ec2:DescribeInstances"


@dataclass(frozen=True)
class RegistryEntry:
    name: str
    description: str
    params_schema: dict[str, Any]
    aws_calls: tuple[AwsCall, ...]
    handler: Callable[..., dict[str, Any]]


def _client(service: str, session: boto3.Session | None = None):
    return (session or boto3).client(service)


def _response_count(response: dict[str, Any]) -> int:
    """Best-effort "how many results" for an audit entry: the length of
    whichever top-level field in the response is a list, or 1 if none is."""
    for value in response.values():
        if isinstance(value, list):
            return len(value)
    return 1


def _response_preview(response: Any, limit: int = 200) -> str:
    """A short, truncated preview of a response, for audit review purposes.

    Every registry capability is already curated to be non-sensitive (that's
    the whole reason IAM only grants these specific actions), so previewing
    the actual response is low-risk today. Worth revisiting once dynamic
    dispatch exists — a curated but broader set of calls may need more care
    here than a blind string truncation.
    """
    text = str(response)
    return text if len(text) <= limit else text[:limit] + "..."


def _failure_preview(exc: Exception) -> str:
    """A short, safe reason an AWS call failed, for audit review — AWS's own
    error *code* (e.g. "AccessDenied", "UnauthorizedOperation") when this is a
    real ClientError, never its full message verbatim: botocore's error
    messages sometimes echo back a caller-supplied parameter value, and the
    whole point of not storing full responses (this module's own
    ``_response_preview``, and docs/context.md section 7.5) is not undoing
    that carefulness on the failure path just because it's a different field.
    Anything else (e.g. ParamValidationError, which isn't a ClientError) falls
    back to its exception class name — still informative, still bounded.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code:
            return code
    return type(exc).__name__


# The error codes AWS uses, across different services, for "you don't have
# permission for this" — checked reactively in agent/router.py for a real
# AccessDenied our own proactive permission gate (agent/permissions.py)
# didn't anticipate. Not exhaustive by design: anything not in this set still
# gets a safe, generic failure message rather than a wrong specific one.
ACCESS_DENIED_ERROR_CODES = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
)


def audited_call(client, operation: str, audit: AuditCallback | None = None, **params) -> Any:
    """Call one boto3 operation directly, optionally reporting it for audit.

    This is the one place a real, single AWS request gets made in this module
    outside of ``_paginated`` (which audits its own merged, multi-page fetch as
    one logical call — see its docstring). Anything called through here that
    runs more than once for one question (e.g. once per bucket) produces one
    audit entry per call, since each is a genuinely separate request about a
    different resource, not a mechanical continuation of the same one.
    """
    service_name = client.meta.service_model.service_name
    start = time.monotonic()
    try:
        response = getattr(client, operation)(**params)
    except Exception as exc:
        if audit:
            audit(
                AwsCallRecord(
                    service_name,
                    operation,
                    params,
                    False,
                    None,
                    _failure_preview(exc),
                    time.monotonic() - start,
                )
            )
        raise
    if audit:
        audit(
            AwsCallRecord(
                service_name,
                operation,
                params,
                True,
                _response_count(response),
                _response_preview(response),
                time.monotonic() - start,
            )
        )
    return response


def _paginated(
    client, operation: str, audit: AuditCallback | None = None, **params
) -> dict[str, Any]:
    """Run a paginated boto3 operation and merge every page into one result.

    Several of our operations return partial results with a token for the next
    page once an account has enough resources. Fetching only the first page
    and treating it as complete would silently under-report — the same failure
    mode the fallback path is explicitly designed to avoid (docs/context.md
    section 7.2). ``build_full_result`` merges each page's list fields for us.

    Audited as *one* logical call regardless of how many pages it took: page
    fetching is a mechanical continuation of one request our code made once,
    not a separate decision each time, so it shouldn't fragment into one audit
    entry per page.
    """
    service_name = client.meta.service_model.service_name
    start = time.monotonic()
    try:
        result = client.get_paginator(operation).paginate(**params).build_full_result()
    except Exception as exc:
        if audit:
            audit(
                AwsCallRecord(
                    service_name,
                    operation,
                    params,
                    False,
                    None,
                    _failure_preview(exc),
                    time.monotonic() - start,
                )
            )
        raise
    if audit:
        audit(
            AwsCallRecord(
                service_name,
                operation,
                params,
                True,
                _response_count(result),
                _response_preview(result),
                time.monotonic() - start,
            )
        )
    return result


def ec2_instances(
    session: boto3.Session | None = None,
    audit: AuditCallback | None = None,
    state: str | None = None,
    min_uptime_hours: float | None = None,
) -> dict[str, Any]:
    """List EC2 instances with their key attributes — state, uptime, instance
    type, AMI, IPs, VPC, availability zone, security groups, tags, and
    attached EBS volumes — from a single ``describe_instances`` call.

    ``state`` defaults to "running" (the common case); "stopped" or "all"
    broadens it. ``min_uptime_hours`` filters to running instances up at
    least that long (e.g. "which instances have been running more than 24
    hours?") — applied to our own already-fetched result, not as an AWS API
    parameter, since ``DescribeInstances`` has no such filter itself.
    Meaningless for a non-running instance, so it only ever narrows the
    running subset, regardless of ``state``.
    """
    if min_uptime_hours is not None:
        min_uptime_hours = float(min_uptime_hours)
    state_filter = (state or "running").lower()
    if state_filter not in ("running", "stopped", "all"):
        state_filter = "running"
    ec2 = _client("ec2", session)
    now = datetime.now(UTC)
    instances = []
    reservations = _paginated(ec2, "describe_instances", audit=audit)["Reservations"]
    for reservation in reservations:
        for instance in reservation["Instances"]:
            instance_state = instance["State"]["Name"]
            if state_filter != "all" and instance_state != state_filter:
                continue
            uptime_hours = None
            if instance_state == "running":
                launch_time = instance["LaunchTime"]
                uptime_hours = round((now - launch_time).total_seconds() / 3600, 1)
                if min_uptime_hours is not None and uptime_hours < min_uptime_hours:
                    continue
            elif min_uptime_hours is not None:
                continue
            tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
            security_groups = [
                sg.get("GroupName") or sg["GroupId"] for sg in instance.get("SecurityGroups", [])
            ]
            volume_ids = [
                bdm["Ebs"]["VolumeId"]
                for bdm in instance.get("BlockDeviceMappings", [])
                if "Ebs" in bdm
            ]
            instances.append(
                {
                    "instance_id": instance["InstanceId"],
                    "state": instance_state,
                    "instance_type": instance.get("InstanceType"),
                    "ami_id": instance.get("ImageId"),
                    "public_ip": instance.get("PublicIpAddress"),
                    "private_ip": instance.get("PrivateIpAddress"),
                    "vpc_id": instance.get("VpcId"),
                    "availability_zone": instance.get("Placement", {}).get("AvailabilityZone"),
                    "security_groups": security_groups,
                    "name_tag": tags.get("Name"),
                    "volume_ids": volume_ids,
                    "launch_time": instance["LaunchTime"].isoformat()
                    if "LaunchTime" in instance
                    else None,
                    "uptime_hours": uptime_hours,
                }
            )
    return {
        "instances": instances,
        "state_filter": state_filter,
        "min_uptime_hours": min_uptime_hours,
    }


def security_groups_open_to_internet(
    session: boto3.Session | None = None, audit: AuditCallback | None = None
) -> dict[str, Any]:
    """List security groups with an inbound rule open to the whole internet (0.0.0.0/0)."""
    ec2 = _client("ec2", session)
    flagged = []
    for group in _paginated(ec2, "describe_security_groups", audit=audit)["SecurityGroups"]:
        open_rule_count = sum(
            1
            for permission in group.get("IpPermissions", [])
            for ip_range in permission.get("IpRanges", [])
            if ip_range.get("CidrIp") == "0.0.0.0/0"
        )
        if open_rule_count:
            flagged.append(
                {
                    "group_id": group["GroupId"],
                    "group_name": group.get("GroupName", ""),
                    "open_rule_count": open_rule_count,
                }
            )
    return {"flagged_groups": flagged}


def s3_public_access(
    session: boto3.Session | None = None, audit: AuditCallback | None = None
) -> dict[str, Any]:
    """List S3 buckets and whether each one blocks public access."""
    s3 = _client("s3", session)
    buckets = []
    for bucket in _paginated(s3, "list_buckets", audit=audit)["Buckets"]:
        name = bucket["Name"]
        try:
            config = audited_call(s3, "get_public_access_block", audit, Bucket=name)[
                "PublicAccessBlockConfiguration"
            ]
            blocks_public_access = all(config.values())
        except s3.exceptions.ClientError:
            # No Public Access Block configuration set at all means nothing is blocked.
            blocks_public_access = False
        buckets.append({"bucket": name, "blocks_public_access": blocks_public_access})
    return {"buckets": buckets}


def rds_instances(
    session: boto3.Session | None = None, audit: AuditCallback | None = None
) -> dict[str, Any]:
    """List RDS database instances with engine, version, storage, encryption,
    backup retention, Multi-AZ, instance class, status, and endpoint.

    Promoted from dispatch's generic fallback to a hand-formatted registry
    entry (Session 3) — the same data was already reachable via
    ``rds:DescribeDBInstances`` through dispatch, just rendered as a raw
    Python-dict dump rather than a readable answer.
    """
    rds = _client("rds", session)
    result = _paginated(rds, "describe_db_instances", audit=audit)
    instances = [
        {
            "db_instance_identifier": db["DBInstanceIdentifier"],
            "engine": db.get("Engine"),
            "engine_version": db.get("EngineVersion"),
            "status": db.get("DBInstanceStatus"),
            "instance_class": db.get("DBInstanceClass"),
            "allocated_storage_gb": db.get("AllocatedStorage"),
            "storage_encrypted": db.get("StorageEncrypted"),
            "multi_az": db.get("MultiAZ"),
            "backup_retention_days": db.get("BackupRetentionPeriod"),
            "endpoint": db.get("Endpoint", {}).get("Address"),
            "create_time": (
                db["InstanceCreateTime"].isoformat() if "InstanceCreateTime" in db else None
            ),
        }
        for db in result["DBInstances"]
    ]
    return {"instances": instances}


def rds_clusters(
    session: boto3.Session | None = None, audit: AuditCallback | None = None
) -> dict[str, Any]:
    """List RDS clusters (e.g. Aurora) with engine, version, encryption,
    backup retention, Multi-AZ, status, and endpoint. Same promotion from
    dispatch as ``rds_instances`` above, for the same reason."""
    rds = _client("rds", session)
    result = _paginated(rds, "describe_db_clusters", audit=audit)
    clusters = [
        {
            "db_cluster_identifier": c["DBClusterIdentifier"],
            "engine": c.get("Engine"),
            "engine_version": c.get("EngineVersion"),
            "status": c.get("Status"),
            "storage_encrypted": c.get("StorageEncrypted"),
            "multi_az": c.get("MultiAZ"),
            "backup_retention_days": c.get("BackupRetentionPeriod"),
            "endpoint": c.get("Endpoint"),
            "create_time": (
                c["ClusterCreateTime"].isoformat() if "ClusterCreateTime" in c else None
            ),
        }
        for c in result["DBClusters"]
    ]
    return {"clusters": clusters}


def iam_roles_users_and_groups(
    session: boto3.Session | None = None, audit: AuditCallback | None = None
) -> dict[str, Any]:
    """List existing IAM roles, users, and groups."""
    iam = _client("iam", session)
    roles = [role["RoleName"] for role in _paginated(iam, "list_roles", audit=audit)["Roles"]]
    users = [user["UserName"] for user in _paginated(iam, "list_users", audit=audit)["Users"]]
    groups = [group["GroupName"] for group in _paginated(iam, "list_groups", audit=audit)["Groups"]]
    return {"roles": roles, "users": users, "groups": groups}


def cost_by_service_this_month(
    session: boto3.Session | None = None, audit: AuditCallback | None = None
) -> dict[str, Any]:
    """Cost broken down by AWS service, for the current month so far.

    GetCostAndUsage has no boto3-native paginator (unlike the other four calls
    in this module), so pagination is handled by hand: keep following
    ``NextPageToken`` until the response stops returning one. Audited as one
    logical call for the whole loop, same as ``_paginated`` — see its docstring
    for why mechanical pagination shouldn't fragment the audit log.
    """
    ce = _client("ce", session)
    today = datetime.now(UTC).date()
    start = today.replace(day=1)
    base_params = {
        "TimePeriod": {"Start": start.isoformat(), "End": today.isoformat()},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    groups = []
    next_page_token = None
    service_name = ce.meta.service_model.service_name
    call_start = time.monotonic()
    try:
        while True:
            request = dict(base_params)
            if next_page_token:
                request["NextPageToken"] = next_page_token
            response = ce.get_cost_and_usage(**request)
            groups.extend(response["ResultsByTime"][0]["Groups"])
            next_page_token = response.get("NextPageToken")
            if not next_page_token:
                break
    except Exception as exc:
        if audit:
            audit(
                AwsCallRecord(
                    service_name,
                    "get_cost_and_usage",
                    base_params,
                    False,
                    None,
                    _failure_preview(exc),
                    time.monotonic() - call_start,
                )
            )
        raise
    if audit:
        audit(
            AwsCallRecord(
                service_name,
                "get_cost_and_usage",
                base_params,
                True,
                len(groups),
                _response_preview(groups),
                time.monotonic() - call_start,
            )
        )
    breakdown = [
        {
            "service": group["Keys"][0],
            "cost_usd": round(float(group["Metrics"]["UnblendedCost"]["Amount"]), 2),
        }
        for group in groups
    ]
    return {"start_date": start.isoformat(), "end_date": today.isoformat(), "breakdown": breakdown}


REGISTRY: tuple[RegistryEntry, ...] = (
    RegistryEntry(
        name="ec2_instances",
        description=(
            "List EC2 instances and their key attributes: state, uptime, instance type, AMI, "
            "public/private IP, VPC, availability zone, security groups, Name tag, and attached "
            "EBS volume IDs. Defaults to running instances; state can be 'stopped' or 'all'. "
            "Accepts an optional minimum-uptime-hours threshold to directly answer questions like "
            "'which instances have been running more than 24 hours?'. Covers ONLY EC2 instances — "
            "never a standalone list of EBS volumes/snapshots, Elastic IP addresses, VPCs, "
            "subnets, or any other EC2-related resource type; use the general-purpose tool for "
            "those instead."
        ),
        params_schema={
            "state": {
                "type": "string",
                "description": "'running' (default), 'stopped', or 'all'.",
            },
            "min_uptime_hours": {
                "type": "number",
                "description": (
                    "Only include running instances up at least this many hours. "
                    "Omit to list every instance matching the state filter."
                ),
            },
        },
        aws_calls=(AwsCall("ec2", "describe_instances", "ec2:DescribeInstances"),),
        handler=ec2_instances,
    ),
    RegistryEntry(
        name="security_groups_open_to_internet",
        description=(
            "List security groups with an inbound rule open to the whole internet (0.0.0.0/0)."
        ),
        params_schema={},
        aws_calls=(AwsCall("ec2", "describe_security_groups", "ec2:DescribeSecurityGroups"),),
        handler=security_groups_open_to_internet,
    ),
    RegistryEntry(
        name="rds_instances",
        description=(
            "List RDS database instances with engine, version, storage size, encryption "
            "status, backup retention period, Multi-AZ status, instance class, status, and "
            "endpoint. Covers ONLY RDS instances — not clusters (a separate tool covers those)."
        ),
        params_schema={},
        aws_calls=(AwsCall("rds", "describe_db_instances", "rds:DescribeDBInstances"),),
        handler=rds_instances,
    ),
    RegistryEntry(
        name="rds_clusters",
        description=(
            "List RDS clusters (e.g. Aurora) with engine, version, encryption status, backup "
            "retention period, Multi-AZ status, status, and endpoint. Covers ONLY RDS clusters — "
            "not standalone instances (a separate tool covers those)."
        ),
        params_schema={},
        aws_calls=(AwsCall("rds", "describe_db_clusters", "rds:DescribeDBClusters"),),
        handler=rds_clusters,
    ),
    RegistryEntry(
        name="s3_public_access",
        description=(
            "List S3 buckets and whether each one blocks public access. Covers ONLY bucket-level "
            "public-access configuration — never the objects/files stored inside a bucket; use "
            "the general-purpose tool to list a bucket's contents instead."
        ),
        params_schema={},
        aws_calls=(
            AwsCall("s3", "list_buckets", "s3:ListAllMyBuckets"),
            AwsCall("s3", "get_public_access_block", "s3:GetBucketPublicAccessBlock"),
        ),
        handler=s3_public_access,
    ),
    RegistryEntry(
        name="iam_roles_users_and_groups",
        description="List existing IAM roles, users, and groups.",
        params_schema={},
        aws_calls=(
            AwsCall("iam", "list_roles", "iam:ListRoles"),
            AwsCall("iam", "list_users", "iam:ListUsers"),
            AwsCall("iam", "list_groups", "iam:ListGroups"),
        ),
        handler=iam_roles_users_and_groups,
    ),
    RegistryEntry(
        name="cost_by_service_this_month",
        description="Cost broken down by AWS service, for the current month so far.",
        params_schema={},
        aws_calls=(AwsCall("ce", "get_cost_and_usage", "ce:GetCostAndUsage"),),
        handler=cost_by_service_this_month,
    ),
)
