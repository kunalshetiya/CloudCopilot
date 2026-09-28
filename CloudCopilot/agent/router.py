"""Routes one plain-English question to the registry, dynamic dispatch, or a
suggestion — the three tiers from docs/context.md section 7.2.

Exactly one OpenAI API call per question, using native tool-calling: every
registry entry becomes a callable "tool," and one further generic tool
(`run_curated_aws_call`) exposes structured dynamic dispatch (agent/dispatch.py).
The model either calls one of these, or replies in plain text — the
suggestion-only fallback, which stays a permanent third tier even now that
dynamic dispatch exists (docs/context.md 7.2 tier 3), not a placeholder for it.
Formatting a hit into a readable answer happens in our own deterministic code,
never a second LLM call, so a question answered by a tool always costs exactly
one request, and its wording never varies between two runs.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from openai import OpenAI

from agent import dispatch, permissions
from agent.audit import AnswerPath, AuditEvent, AuditLog
from agent.registry import (
    ACCESS_DENIED_ERROR_CODES,
    REGISTRY,
    AwsCallRecord,
    RegistryEntry,
    _failure_preview,
)

DISPATCH_TOOL_NAME = "run_curated_aws_call"

# A question can need more than one independent call (e.g. "what EC2 instances
# and VPCs do I have") — this bounds how many one question can ever trigger,
# the same "3-5 calls" cap docs/context.md section 7.2 always intended, now
# actually enforced rather than just documented.
MAX_CALLS_PER_QUESTION = 5

# Added to every tool's schema (registry and dispatch alike) so the model can
# request sorting/limiting over whatever list of results a call returns,
# instead of trying to compute "biggest"/"top N"/"count" itself — our own
# code does the actual sort/slice, deterministically. See docs/context.md
# section 7.2 and learning-notes.md for why this stays out of the model's hands.
SHAPE_PARAM_NAMES = {"sort_by", "sort_descending", "limit"}
SHAPE_PARAMS_SCHEMA = {
    "sort_by": {
        "type": "string",
        "description": "Optional: sort results by this field name before answering.",
    },
    "sort_descending": {
        "type": "boolean",
        "description": "Optional: sort highest-first instead of lowest-first.",
    },
    "limit": {
        "type": "integer",
        "description": "Optional: only include this many results, e.g. for a 'top 3' question.",
    },
}

SYSTEM_PROMPT = """You are CloudCopilot, a read-only assistant for a live AWS account.

You have specific named tools for common questions, plus one general-purpose tool
(run_curated_aws_call) for any other read-only AWS operation from a fixed, reviewed list of
(service, operation) pairs, described in that tool's own definition. Use it whenever a question is
a genuine, answerable AWS-operations question that isn't covered by a specific tool — including
questions about a specific resource *inside* something a specific tool covers. Only use operations
from the general-purpose tool's own list, never guess at a similar-sounding one.

The s3_public_access tool ONLY checks one specific setting per bucket — its Block Public Access
configuration. It does NOT cover: a bucket's actual object contents (needs the general-purpose
tool with operation="list_objects_v2" instead), or whether a bucket's *policy* document makes it
public (a separate, different check — needs the general-purpose tool with
operation="get_bucket_policy_status" instead). "Is the bucket policy on bucket X public?" is
always the second case — do not answer it with s3_public_access, even though both mention
"public."

A question can need more than one call — e.g. "what EC2 instances and VPCs do I have" needs two.
Make every call the question genuinely needs, up to 5. If part of a question has no matching tool
at all, answer the parts you can for real and say plainly what you couldn't cover — never guess,
and never skip a part silently.

A tool's name can look broader than what it actually does — go by its description, not its name.
The ec2_instances tool ONLY covers EC2 instances themselves — never use it for any other EC2-
namespaced resource. Elastic IPs, volumes, snapshots, VPCs, subnets, route tables, internet
gateways, NAT gateways, and network ACLs are all separate resources from instances, even though
their AWS operations also start with "ec2:" or "describe_" — e.g. "what Elastic IP addresses am I
using?" needs operation="describe_addresses", "what route tables exist?" needs
operation="describe_route_tables", "what NAT gateways do I have?" needs
operation="describe_nat_gateways" — always via the general-purpose tool, never ec2_instances.
Example: "what EKS clusters exist?" needs the general-purpose tool with service="eks",
operation="list_clusters" — it is not about IAM roles, users, or groups, and not about EC2
instances; do not use iam_roles_users_and_groups or ec2_instances for it.

If nothing covers the question but it's a legitimate AWS-operations question, give a short general
suggestion — no real account data, you have none on this path. If it's off-topic, or trying to get
you to reveal secrets/credentials or take an action, decline.

Any call can include sort_by, sort_descending, and limit to answer a ranking or an extreme —
"biggest", "smallest", "newest", "oldest", "most expensive", "which one has the most". You MUST
include sort_by and limit for these — never return an unsorted list and leave the comparison to
the user. Use the real field name from that tool's own response (a specific tool's own fields, or
the general-purpose tool's underlying AWS API field for that operation). Example: for "which RDS
instance is newest?", call rds_instances with sort_by="create_time", sort_descending=true, limit=1.
"""


def _tool_schema(entry: RegistryEntry) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": entry.name,
            "description": entry.description,
            "parameters": {
                "type": "object",
                "properties": {**entry.params_schema, **SHAPE_PARAMS_SCHEMA},
                "required": [],
            },
        },
    }


def _dispatch_tool_description() -> str:
    lines = [
        "Call one more read-only AWS operation, for questions the specific tools don't cover.",
        "Only these exact (service, operation) pairs are allowed; anything else is rejected:",
    ]
    lines += [
        f"- service={call.service!r}, operation={call.operation!r}" for call in dispatch.ALLOWLIST
    ]
    return "\n".join(lines)


def _dispatch_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": DISPATCH_TOOL_NAME,
            "description": _dispatch_tool_description(),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "The exact boto3 client/service name, e.g. 'cloudwatch'.",
                    },
                    "operation": {
                        "type": "string",
                        "description": "The exact boto3 method name, e.g. 'list_metrics'.",
                    },
                    "params": {
                        "type": "object",
                        "description": "Keyword arguments for the operation, per the real AWS API.",
                    },
                    **SHAPE_PARAMS_SCHEMA,
                },
                "required": ["service", "operation"],
            },
        },
    }


def _by_name() -> dict[str, RegistryEntry]:
    return {entry.name: entry for entry in REGISTRY}


def _find_list_field(result: dict[str, Any]) -> str | None:
    for key, value in result.items():
        if isinstance(value, list):
            return key
    return None


def _shape(
    result: dict[str, Any],
    sort_by: str | None = None,
    sort_descending: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    field = _find_list_field(result)
    if field is None:
        return result
    items = result[field]
    if sort_by is not None:
        try:
            items = sorted(
                items,
                key=lambda item: item.get(sort_by) if isinstance(item, dict) else None,
                reverse=bool(sort_descending),
            )
        except TypeError:
            pass  # unsortable values for this field — leave the order as-is rather than crash
    if limit is not None:
        items = items[: int(limit)]
    shaped = dict(result)
    shaped[field] = items
    return shaped


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def _format_hours(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def _format_ec2_instances(result: dict[str, Any]) -> str:
    instances = result["instances"]
    threshold = result.get("min_uptime_hours")
    state_filter = result.get("state_filter", "running")
    if threshold is not None:
        scope = f"running more than {_format_hours(threshold)}h"
    elif state_filter == "all":
        scope = "in any state"
    else:
        scope = state_filter
    if not instances:
        return f"No EC2 instances found ({scope})."
    lines = []
    for i in instances:
        bits = [i["state"]]
        if i["uptime_hours"] is not None:
            bits.append(f"{i['uptime_hours']}h uptime")
        if i["instance_type"]:
            bits.append(i["instance_type"])
        if i["ami_id"]:
            bits.append(f"ami={i['ami_id']}")
        if i["vpc_id"]:
            bits.append(f"vpc={i['vpc_id']}")
        if i["availability_zone"]:
            bits.append(i["availability_zone"])
        bits.append(f"public IP {i['public_ip']}" if i["public_ip"] else "no public IP")
        if i["security_groups"]:
            bits.append("SGs: " + ", ".join(i["security_groups"]))
        if i["volume_ids"]:
            bits.append(f"{len(i['volume_ids'])} volume(s)")
        bits.append(f"name={i['name_tag']}" if i["name_tag"] else "no Name tag")
        lines.append(f"{i['instance_id']}: " + ", ".join(bits))
    return f"{len(instances)} EC2 instance(s) ({scope}):\n" + _bullets(lines)


def _format_security_groups(result: dict[str, Any]) -> str:
    flagged = result["flagged_groups"]
    if not flagged:
        return "No security groups allow inbound traffic from the whole internet."
    lines = [
        f"{g['group_name']} ({g['group_id']}): {g['open_rule_count']} rule(s) open to 0.0.0.0/0"
        for g in flagged
    ]
    header = f"{len(flagged)} security group(s) allow inbound traffic from the whole internet:"
    return f"{header}\n" + _bullets(lines)


def _format_s3_public_access(result: dict[str, Any]) -> str:
    buckets = result["buckets"]
    if not buckets:
        return "No S3 buckets found."
    lines = [
        f"{b['bucket']}: "
        + ("blocks" if b["blocks_public_access"] else "does NOT block")
        + " public access"
        for b in buckets
    ]
    return f"{len(buckets)} S3 bucket(s) found:\n" + _bullets(lines)


def _format_iam_roles_users_and_groups(result: dict[str, Any]) -> str:
    sections = []
    for label, items in (
        ("role", result["roles"]),
        ("user", result["users"]),
        ("group", result["groups"]),
    ):
        if items:
            sections.append(f"{len(items)} {label}(s):\n" + _bullets(items))
        else:
            sections.append(f"0 {label}s.")
    return "\n\n".join(sections)


def _format_cost_by_service(result: dict[str, Any]) -> str:
    breakdown = result["breakdown"]
    period = f"{result['start_date']} to {result['end_date']}"
    if not breakdown:
        return f"No cost data recorded yet for {period}."
    lines = [f"{b['service']}: ${b['cost_usd']}" for b in breakdown]
    return f"Cost by service, {period}:\n" + _bullets(lines)


def _format_rds_instances(result: dict[str, Any]) -> str:
    instances = result["instances"]
    if not instances:
        return "No RDS instances found."
    lines = []
    for db in instances:
        bits = [b for b in (db["engine"], db["engine_version"], db["status"]) if b]
        if db["instance_class"]:
            bits.append(db["instance_class"])
        if db["allocated_storage_gb"] is not None:
            bits.append(f"{db['allocated_storage_gb']}GB")
        bits.append("encrypted" if db["storage_encrypted"] else "not encrypted")
        if db["multi_az"]:
            bits.append("Multi-AZ")
        if db["backup_retention_days"] is not None:
            bits.append(f"{db['backup_retention_days']}d backup retention")
        if db["endpoint"]:
            bits.append(f"endpoint={db['endpoint']}")
        lines.append(f"{db['db_instance_identifier']}: " + ", ".join(bits))
    return f"{len(instances)} RDS instance(s):\n" + _bullets(lines)


def _format_rds_clusters(result: dict[str, Any]) -> str:
    clusters = result["clusters"]
    if not clusters:
        return "No RDS clusters found."
    lines = []
    for c in clusters:
        bits = [b for b in (c["engine"], c["engine_version"], c["status"]) if b]
        bits.append("encrypted" if c["storage_encrypted"] else "not encrypted")
        if c["multi_az"]:
            bits.append("Multi-AZ")
        if c["backup_retention_days"] is not None:
            bits.append(f"{c['backup_retention_days']}d backup retention")
        if c["endpoint"]:
            bits.append(f"endpoint={c['endpoint']}")
        lines.append(f"{c['db_cluster_identifier']}: " + ", ".join(bits))
    return f"{len(clusters)} RDS cluster(s):\n" + _bullets(lines)


_FORMATTERS = {
    "ec2_instances": _format_ec2_instances,
    "security_groups_open_to_internet": _format_security_groups,
    "rds_instances": _format_rds_instances,
    "rds_clusters": _format_rds_clusters,
    "s3_public_access": _format_s3_public_access,
    "iam_roles_users_and_groups": _format_iam_roles_users_and_groups,
    "cost_by_service_this_month": _format_cost_by_service,
}


def _format_dispatch_result(result: dict[str, Any]) -> str:
    """Generic formatting for dynamic dispatch — unlike the registry's
    hand-worded formatters, this can't know the shape or meaning of an
    arbitrary curated AWS response in advance, so it stays intentionally raw."""
    field = _find_list_field(result)
    if field is None:
        base = str({k: v for k, v in result.items() if k != "_truncated"})
        if result.get("_truncated"):
            base += " (showing a partial, truncated result — there may be more)"
        return base
    items = result[field]
    if not items:
        return "No results found."
    header = f"{len(items)} result(s) found"
    if result.get("_truncated"):
        header += " (showing a partial, truncated result — there may be more)"
    return f"{header}:\n" + _bullets(str(item) for item in items)


def _make_audit_callback(audit_log: AuditLog, question: str, question_id: str, path: AnswerPath):
    def callback(record: AwsCallRecord) -> None:
        audit_log.record(
            AuditEvent(
                question=question,
                question_id=question_id,
                path=path,
                timestamp=datetime.now(UTC).isoformat(),
                duration_seconds=record.duration_seconds,
                success=record.success,
                service=record.service,
                operation=record.operation,
                params=record.params,
                result_count=record.result_count,
                preview=record.preview,
            )
        )

    return callback


def _pop_shape_args(params: dict[str, Any]) -> dict[str, Any]:
    return {name: params.pop(name) for name in list(params) if name in SHAPE_PARAM_NAMES}


PERMISSION_GAP_SUGGESTION_PROMPT = """You are CloudCopilot, a read-only AWS operations assistant.
The user asked a legitimate AWS-operations question, but this agent's current AWS role does not
have permission to answer it directly. In one short, plain-language sentence, suggest how they
might find this out another way (e.g. a specific AWS console page or CLI command), using only
general AWS knowledge — never implying you have access to this account's actual data."""


def _permission_gap_message(missing: set[str]) -> str:
    actions = ", ".join(sorted(missing))
    return (
        f"I can't answer that — it needs `{actions}`, which isn't part of this agent's "
        "currently granted AWS role."
    )


def _permission_gap_suggestion(question: str, client: OpenAI, model: str) -> str:
    # A second, short LLM call — a deliberate, narrow exception to the
    # one-call-per-question rule (docs/context.md 7.2), made only for this
    # specific case. A failure here must never hide the (already-determined)
    # permission-gap message itself, so any error just means no suggestion.
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": PERMISSION_GAP_SUGGESTION_PROMPT},
                {"role": "user", "content": question},
            ],
        )
        return response.choices[0].message.content or ""
    except Exception:
        return ""


def _record_permission_gap(
    audit_log: AuditLog,
    question: str,
    question_id: str,
    path: AnswerPath,
    service: str,
    operation: str,
    missing: set[str],
) -> None:
    audit_log.record(
        AuditEvent(
            question=question,
            question_id=question_id,
            path=path,
            timestamp=datetime.now(UTC).isoformat(),
            duration_seconds=0.0,
            success=False,
            service=service,
            operation=operation,
            preview=f"blocked: missing IAM action(s) {', '.join(sorted(missing))}",
        )
    )


def _permission_gap_answer(question: str, missing: set[str], client: OpenAI, model: str) -> str:
    message = _permission_gap_message(missing)
    suggestion = _permission_gap_suggestion(question, client, model)
    return f"{message}\n\n{suggestion}" if suggestion else message


def _dispatch_not_allowed_message(exc: dispatch.DispatchNotAllowed) -> str:
    """Two genuinely different situations, worded differently rather than
    collapsed into one flat "can't do that": DENYLIST's own reason names a
    deliberate security decision; no reason at all means this operation was
    simply never reviewed onto ALLOWLIST — not restricted, just not built.
    No suggestion here (unlike the permission-gap answers) — this is a fixed
    policy decision, not a deployment gap the user could resolve by getting
    more IAM permissions."""
    if exc.reason:
        return (
            f"I can't make that particular AWS call — it's restricted for security reasons "
            f"({exc.reason})."
        )
    return (
        "I can't make that particular AWS call — it hasn't been reviewed and approved for use yet."
    )


def _execute_tool_call(
    call,
    question: str,
    question_id: str,
    audit_log: AuditLog,
    aws_session: boto3.Session | None,
    client: OpenAI,
    model: str,
    granted_actions: frozenset[str] | None,
) -> str:
    """Run one matched tool call (registry or dispatch) through every existing
    safety check and return its formatted answer — the exact same path a
    single-call question always used, now factored out so a question needing
    several independent calls (docs/context.md 7.2) can run each one through
    it identically. Nothing about the per-call safety checks (the permission
    gate, the dispatch allow/deny-list, the reactive AccessDenied handling)
    changes when there's more than one call in the same question.
    """
    params = json.loads(call.function.arguments or "{}")
    shape_args = _pop_shape_args(params)

    if call.function.name == DISPATCH_TOOL_NAME:
        service = params.get("service")
        operation = params.get("operation")
        inner_params = params.get("params") or {}
        matched = dispatch.find_call(service, operation)
        if matched is not None:
            missing = permissions.missing_actions([matched.iam_action], granted=granted_actions)
            if missing:
                _record_permission_gap(
                    audit_log, question, question_id, "fallback", service, operation, missing
                )
                return _permission_gap_answer(question, missing, client, model)
        audit = _make_audit_callback(audit_log, question, question_id, path="fallback")
        try:
            result = dispatch.run_curated_call(
                service, operation, inner_params, session=aws_session, audit=audit
            )
        except dispatch.DispatchNotAllowed as exc:
            return _dispatch_not_allowed_message(exc)
        except Exception as exc:
            # matched is guaranteed non-None here: an unmatched (service,
            # operation) would already have raised DispatchNotAllowed above,
            # never reaching a real AWS call at all.
            if matched is not None and _failure_preview(exc) in ACCESS_DENIED_ERROR_CODES:
                return _permission_gap_answer(question, {matched.iam_action}, client, model)
            return "I couldn't complete that AWS call right now. Please try again shortly."
        result = _shape(result, **shape_args) if shape_args else result
        return _format_dispatch_result(result)

    entry = _by_name().get(call.function.name)
    if entry is None:
        return "I'm not sure how to help with that."
    missing = permissions.missing_actions(
        (c.iam_action for c in entry.aws_calls), granted=granted_actions
    )
    if missing:
        _record_permission_gap(
            audit_log,
            question,
            question_id,
            "registry",
            entry.aws_calls[0].service,
            entry.name,
            missing,
        )
        return _permission_gap_answer(question, missing, client, model)
    audit = _make_audit_callback(audit_log, question, question_id, path="registry")
    try:
        result = entry.handler(session=aws_session, audit=audit, **params)
    except Exception as exc:
        if _failure_preview(exc) in ACCESS_DENIED_ERROR_CODES:
            # Not necessarily the exact one of entry.aws_calls that failed
            # (a multi-call capability doesn't tell us which) — naming all
            # of them is still honest and more useful than a generic
            # "try again," which would be actively misleading here.
            entry_actions = {c.iam_action for c in entry.aws_calls}
            return _permission_gap_answer(question, entry_actions, client, model)
        return "I couldn't complete that AWS call right now. Please try again shortly."
    result = _shape(result, **shape_args) if shape_args else result
    return _FORMATTERS[entry.name](result)


def answer_question(
    question: str,
    *,
    audit_log: AuditLog,
    aws_session: boto3.Session | None = None,
    openai_client: OpenAI | None = None,
    granted_actions: frozenset[str] | None = None,
) -> str:
    """Answer one question: exactly one OpenAI call decides everything needed
    to answer it, then zero or more real AWS calls (up to
    ``MAX_CALLS_PER_QUESTION``) through matched registry entries and/or
    curated dynamic dispatch — independent calls the model can decide on in
    that one turn (e.g. "what EC2 instances and VPCs do I have" needs two),
    not a multi-turn agentic loop. Each call's own outcome (success, a
    permission gap, a security restriction) is reported on its own, so a
    question with one answerable part and one blocked part gets a real
    answer for the first and an honest explanation for the second, rather
    than refusing the whole question. Falls back to a suggestion-only reply
    when no tool matches at all.

    ``granted_actions`` defaults to the real, currently-deployed role's grant
    (``agent.permissions.GRANTED_ACTIONS`` — docs/context.md section 8). A
    matched capability whose required action(s) aren't granted is declined
    before any AWS call is attempted, rather than discovered via
    ``AccessDenied``. Tests that want to exercise a capability's own behavior,
    independent of today's deployment constraints, pass their own set here.
    """
    client = openai_client or OpenAI()
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    question_id = str(uuid.uuid4())

    start = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        tools=[_tool_schema(entry) for entry in REGISTRY] + [_dispatch_tool_schema()],
    )
    llm_duration = time.monotonic() - start
    message = response.choices[0].message

    if message.tool_calls:
        calls = message.tool_calls[:MAX_CALLS_PER_QUESTION]
        answers = [
            _execute_tool_call(
                call, question, question_id, audit_log, aws_session, client, model, granted_actions
            )
            for call in calls
        ]
        combined = "\n\n".join(answers)
        if len(message.tool_calls) > MAX_CALLS_PER_QUESTION:
            combined += (
                f"\n\n(Showing results for the first {MAX_CALLS_PER_QUESTION} of "
                f"{len(message.tool_calls)} operations this question would have needed.)"
            )
        return combined

    answer = message.content or message.refusal or "I'm not sure how to help with that."
    audit_log.record(
        AuditEvent(
            question=question,
            question_id=question_id,
            path="suggestion",
            timestamp=datetime.now(UTC).isoformat(),
            duration_seconds=llm_duration,
            success=True,
        )
    )
    return answer
