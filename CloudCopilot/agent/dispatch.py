"""Structured dynamic dispatch — the fallback path for AWS read operations the
fixed registry doesn't cover (docs/context.md section 7.2, tier 2).

The AI never writes or runs code here — it only ever supplies a (service,
operation, params) triple. Our own code is the only thing that ever calls
``getattr(boto3_client, operation)(**params)``, and only after checking the
operation against a curated, one-by-one ALLOWLIST (never a naming-pattern
guess) and an explicit DENYLIST of "looks safe, isn't" cases.

Deliberately scoped narrow for v1: one AWS operation per question (its
pagination is followed and treated as one logical call, same as the
registry's own ``_paginated``). A question that genuinely needs several
different calls chained together (e.g. "which S3 bucket is biggest" — bucket
sizes aren't in ListBuckets at all) is out of scope on purpose, not silently
mishandled — see docs/context.md section 7.7.
"""

from __future__ import annotations

import time
from typing import Any

import boto3

from agent.registry import (
    AuditCallback,
    AwsCall,
    AwsCallRecord,
    _failure_preview,
    _response_preview,
)

# Hard cap on items fetched across all pages of one dispatch call. Unlike the
# registry's five known, naturally-small operations, dispatch covers
# arbitrary curated-but-uncertain-scale operations (e.g. ListMetrics or
# DescribeSnapshots can return thousands of results in a busy account) —
# fetching without a ceiling here is a real, not just theoretical, risk.
MAX_ITEMS = 200

ALLOWLIST: tuple[AwsCall, ...] = (
    AwsCall("cloudwatch", "list_metrics", "cloudwatch:ListMetrics"),
    AwsCall("cloudwatch", "get_metric_statistics", "cloudwatch:GetMetricStatistics"),
    AwsCall("cloudwatch", "get_metric_data", "cloudwatch:GetMetricData"),
    AwsCall("ec2", "describe_volumes", "ec2:DescribeVolumes"),
    AwsCall("ec2", "describe_snapshots", "ec2:DescribeSnapshots"),
    AwsCall("ec2", "describe_vpcs", "ec2:DescribeVpcs"),
    AwsCall("ec2", "describe_addresses", "ec2:DescribeAddresses"),
    AwsCall("ec2", "describe_regions", "ec2:DescribeRegions"),
    AwsCall("ec2", "describe_subnets", "ec2:DescribeSubnets"),
    AwsCall("ec2", "describe_route_tables", "ec2:DescribeRouteTables"),
    AwsCall("ec2", "describe_internet_gateways", "ec2:DescribeInternetGateways"),
    AwsCall("ec2", "describe_nat_gateways", "ec2:DescribeNatGateways"),
    AwsCall("ec2", "describe_network_acls", "ec2:DescribeNetworkAcls"),
    AwsCall("rds", "describe_db_snapshots", "rds:DescribeDBSnapshots"),
    # rds:DescribeDBInstances/DescribeDBClusters used to be here — promoted to
    # hand-formatted registry entries (Session 3, agent/registry.py's
    # rds_instances/rds_clusters) once they became common enough to deserve
    # real formatting instead of a raw dispatch dict dump. Kept off this list
    # now so the same action isn't reachable two ways at once.
    AwsCall("eks", "list_clusters", "eks:ListClusters"),
    AwsCall("eks", "describe_cluster", "eks:DescribeCluster"),
    AwsCall("ecr", "describe_repositories", "ecr:DescribeRepositories"),
    # elbv2 is the boto3 client name, but its IAM actions use the
    # "elasticloadbalancing" prefix instead — verified against AWS's
    # Service Authorization Reference, not assumed from the client name.
    AwsCall("elbv2", "describe_load_balancers", "elasticloadbalancing:DescribeLoadBalancers"),
    AwsCall("elbv2", "describe_target_groups", "elasticloadbalancing:DescribeTargetGroups"),
    AwsCall("elbv2", "describe_target_health", "elasticloadbalancing:DescribeTargetHealth"),
    AwsCall("autoscaling", "describe_auto_scaling_groups", "autoscaling:DescribeAutoScalingGroups"),
    # s3:ListBucket (not "ListBucketV2" or similar) is the actual IAM action
    # authorizing both list_objects and list_objects_v2 — verified against
    # AWS's Service Authorization Reference, same "don't infer from the boto3
    # method name" rule as elbv2 above. Post-processed below to flag
    # suspicious-looking object keys before the result is ever returned.
    AwsCall("s3", "list_objects_v2", "s3:ListBucket"),
    AwsCall("s3", "get_bucket_policy_status", "s3:GetBucketPolicyStatus"),
    # Post-processed below into a bare count — see FilterLogEvents' entry
    # missing from DENYLIST and the comment on _count_only for why this one
    # operation is allow-listed despite reading log data.
    AwsCall("logs", "filter_log_events", "logs:FilterLogEvents"),
    AwsCall("logs", "describe_log_groups", "logs:DescribeLogGroups"),
)

# Operations that must never run, however safe they look by name — checked
# even though none of these were ever added to ALLOWLIST, as a deliberate
# second line of defense. One entry, one verified reason each; see
# docs/learning-notes.md for how each was actually confirmed, not assumed.
#
# logs:FilterLogEvents used to be here ("returns raw application log
# content") and stays denied in that general form — logs:GetLogEvents does
# too. FilterLogEvents is allow-listed above only as a special case: this
# module strips its response down to a bare event count before it's ever
# returned (see _count_only), so no actual log message content leaves this
# function. A general-purpose, "redact secrets from arbitrary log text and
# call it safe" mechanism was considered and deliberately rejected — free-text
# redaction has no bound on false negatives, unlike the count-only approach
# taken here (docs/context.md section 8).
DENYLIST: dict[tuple[str, str], str] = {
    ("secretsmanager", "get_secret_value"): "returns an actual secret value",
    ("kms", "decrypt"): "decrypts arbitrary ciphertext",
    ("ssm", "get_parameter"): "SecureString parameters return decrypted plaintext",
    ("ssm", "get_parameters"): "SecureString parameters return decrypted plaintext",
    ("ssm", "get_parameters_by_path"): "SecureString parameters return decrypted plaintext",
    ("ec2", "describe_instance_attribute"): "the userData attribute can contain embedded secrets",
    ("ec2", "get_password_data"): "returns the encrypted Windows administrator password",
    ("logs", "get_log_events"): "returns raw application log content",
    ("s3", "get_object"): "returns actual file contents",
    (
        "lambda",
        "get_function",
    ): "Code.Location is a presigned download URL for the function's source",
    ("sqs", "receive_message"): (
        "not actually read-only — hides messages from other consumers via visibility timeout"
    ),
    ("dynamodb", "scan"): "returns actual table data",
    ("dynamodb", "query"): "returns actual table data",
    ("dynamodb", "get_item"): "returns actual table data",
}


class DispatchNotAllowed(Exception):
    """Raised when a requested (service, operation) isn't on ALLOWLIST, or is
    explicitly on DENYLIST. Never bypassed — this is the actual safety
    boundary dynamic dispatch depends on.

    ``reason`` is DENYLIST's own explanation when this was explicitly denied
    by name; ``None`` means it was simply never reviewed onto ALLOWLIST at
    all — agent/router.py words these two situations differently to the user
    (a deliberate security restriction vs. "not built yet")."""

    def __init__(self, service: str, operation: str, reason: str | None):
        self.service = service
        self.operation = operation
        self.reason = reason
        super().__init__(f"{service}:{operation} is not on the curated allow-list")


def is_allowed(service: str, operation: str) -> bool:
    if (service, operation) in DENYLIST:
        return False
    return any(call.service == service and call.operation == operation for call in ALLOWLIST)


def find_call(service: str, operation: str) -> AwsCall | None:
    """The ALLOWLIST entry for (service, operation), if any — used to look up
    its iam_action for the permission gate in agent/router.py, without that
    module needing to know ALLOWLIST's internal shape."""
    return next(
        (call for call in ALLOWLIST if call.service == service and call.operation == operation),
        None,
    )


# Keyword substrings that make an S3 object key worth a human's second look.
# Best-effort on filenames only, never a guarantee — see docs/context.md
# section 8. Object *contents* are never read regardless of this flag:
# s3:GetObject stays on DENYLIST above, independently of anything here.
SUSPICIOUS_KEY_SUBSTRINGS = (
    "secret",
    "password",
    "passwd",
    "credential",
    "private",
    "token",
    "confidential",
    "backup",
    "dump",
    "ssn",
    "apikey",
    "api_key",
)


def _flag_suspicious_keys(response: dict[str, Any]) -> dict[str, Any]:
    contents = response.get("Contents")
    if not contents:
        return response
    flagged = [
        dict(
            item,
            _flagged=any(s in item.get("Key", "").lower() for s in SUSPICIOUS_KEY_SUBSTRINGS),
        )
        for item in contents
    ]
    return {**response, "Contents": flagged}


def _count_only(count: int, truncated: bool) -> dict[str, Any]:
    """Strips FilterLogEvents down to a bare count. Actual log message content
    never reaches this function's caller, however the result later gets
    formatted, previewed for audit, or handed to the model — only "how many
    events matched" is ever answered. See docs/context.md section 8: raw log
    content itself stays permanently denied; this is the one exception, and
    it's count-only by design, not a redaction of the real content."""
    return {"event_count": count, "_truncated": truncated}


def _find_list_field(response: dict[str, Any]) -> str | None:
    for key, value in response.items():
        if isinstance(value, list):
            return key
    return None


def run_curated_call(
    service: str,
    operation: str,
    params: dict[str, Any] | None = None,
    *,
    session: boto3.Session | None = None,
    audit: AuditCallback | None = None,
) -> dict[str, Any]:
    """Run one curated, allow-listed AWS read operation.

    Returns the merged result (all pages, up to MAX_ITEMS) plus a
    ``_truncated`` flag — never silently drops data past the cap without
    saying so, same principle as the registry's own pagination handling.
    """
    params = params or {}
    if not is_allowed(service, operation):
        # Blocked attempts are still visible in the audit log — "every action
        # visible" includes ones our own safety net stopped, same as an
        # AccessDenied from AWS itself (see docs/context.md section 7.7).
        # DENYLIST's own reason string if this was denylisted by name; None
        # (not a generic string) if simply never reviewed onto ALLOWLIST —
        # the exception carries that distinction through to agent/router.py.
        reason = DENYLIST.get((service, operation))
        preview = reason or "not on the curated allow-list"
        if audit:
            audit(AwsCallRecord(service, operation, params, False, None, preview, 0.0))
        raise DispatchNotAllowed(service, operation, reason)

    client = (session or boto3).client(service)
    start = time.monotonic()
    try:
        if client.can_paginate(operation):
            response, truncated = _paginated_with_cap(client, operation, params)
        else:
            response, truncated = getattr(client, operation)(**params), False
    except Exception as exc:
        if audit:
            audit(
                AwsCallRecord(
                    service,
                    operation,
                    params,
                    False,
                    None,
                    _failure_preview(exc),
                    time.monotonic() - start,
                )
            )
        raise

    field = _find_list_field(response)
    count = len(response[field]) if field else 1

    if (service, operation) == ("logs", "filter_log_events"):
        result = _count_only(count, truncated)
    else:
        result = dict(response)
        result["_truncated"] = truncated
        if (service, operation) == ("s3", "list_objects_v2"):
            result = _flag_suspicious_keys(result)

    if audit:
        audit(
            AwsCallRecord(
                service,
                operation,
                params,
                True,
                count,
                _response_preview(result),
                time.monotonic() - start,
            )
        )
    return result


def _paginated_with_cap(
    client, operation: str, params: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    pages = []
    item_count = 0
    truncated = False
    for page in client.get_paginator(operation).paginate(**params):
        pages.append(page)
        field = _find_list_field(page)
        if field:
            item_count += len(page[field])
        if item_count >= MAX_ITEMS:
            truncated = True
            break
    if not pages:
        return {}, False
    merged = dict(pages[0])
    field = _find_list_field(pages[0])
    if field:
        merged[field] = [item for page in pages for item in page.get(field, [])][:MAX_ITEMS]
    return merged, truncated
