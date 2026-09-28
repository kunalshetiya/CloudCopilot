# CloudCopilot — Project Context

This file is the single source of truth for what this project is, what's been decided, and why.
Read this before assuming anything about scope or architecture. If code and this file disagree,
this file wins until we update it together.

Written for two audiences at once: whoever is building this (technical), and anyone who opens
this repo later without the full backstory (could be less technical, could just be new to this
specific project). Where a term might not be obvious, it's explained in plain language first,
then made precise. A glossary of recurring terms is in section 2.

Last updated: 2026-08-06 (Session 3 — permission gate, both AWS roles wired up, audit dashboard)

## 1. Mission

Build a conversational, read-only AWS operations agent: a program, packaged as a container, that
takes a plain-English question about a live AWS account — things like "which EC2 instances have
been running more than 24 hours?" — and answers it correctly by figuring out which AWS API to
call and calling it. It's paired with a second, small companion service whose only job is to
record every question asked and every AWS call made, as a permanent audit trail.

Two things make this safe to point at a real account: it never modifies anything (every
permission it has is read-only, enforced by AWS itself, not just by our code being well-behaved),
and every action it takes is logged somewhere we can review.

Source: internship mission "Port·05 — CloudOps Copilot: Conversational AWS Operations Agent."
The full original brief is kept word-for-word in `docs/mission-spec.md`, so we can always check
back against exactly what was asked, separate from what we decided to build on top of it.

## 2. Glossary (plain language first, then precise)

- **AWS** — Amazon Web Services, the cloud provider this whole project points at.
- **boto3** — the official Python library for talking to AWS. "A boto3 call" just means "Python
  code asking AWS to do or return something."
- **IAM (Identity and Access Management)** — AWS's system for controlling who, or what, is
  allowed to do what. An **IAM role** is a named bundle of permissions something can use; an
  **IAM policy** is the actual document (written in JSON) that spells out exactly which actions
  are allowed.
- **Least privilege** — giving something only the permissions it actually needs, nothing extra
  "just in case."
- **OIDC (OpenID Connect)** — a way for GitHub Actions to prove its identity directly to AWS and
  receive temporary, short-lived permissions — instead of us storing a long-lived AWS password/key
  as a GitHub secret that could leak or go stale.
- **ECR (Elastic Container Registry)** — AWS's private storage for Docker container images; think
  of it as a private version of Docker Hub.
- **moto** — a Python testing library that pretends to be AWS. It lets us test AWS-calling code
  without touching a real account, waiting on real API calls, or paying for anything.
- **Semgrep** — a tool that scans our source code for risky patterns (hardcoded secrets, unsafe
  code) before it ships.
- **Trivy** — a tool that scans our finished container image for software with known security
  vulnerabilities.
- **Coverage gate** — an automated check that fails the build if too little of the code is
  actually exercised by tests. We require at least 75%.
- **Capability registry** — our name for the list of AWS "questions we know how to answer,"
  where each one is a small, hand-written, hand-tested piece of code.
- **Structured dynamic dispatch** — the fallback system for questions the registry doesn't cover.
  The AI never writes or runs its own code; it only picks a name (which AWS action to try) and
  some parameters, and our own code — the same code path every time — does the actual work of
  calling AWS safely. See section 7.2 for why this distinction matters a lot.
- **Drift-check test** — an automated test that fails the build if the hand-written IAM policy
  and the software's own list of allowed actions ever stop matching each other.

## 3. Repo / working boundaries

- This project lives inside a larger repo: `devops-intern-calfus-org/shruti`, branch `main`.
- Everything for this project stays inside the `CloudCopilot/` folder. Nothing outside it
  (`NodeIQ/`, `bandit/`, repo root files, or `.github/workflows/` files not prefixed
  `CloudCopilot-`) gets touched.
- GitHub Actions workflow files for this project are named `CloudCopilot-*.yml`, matching the
  existing repo convention (e.g. `NodeIQ-tests.yml`).

## 4. Working agreements (how we work, not what we're building)

- Every non-trivial decision gets a real discussion first — options laid out with honest pros and
  cons, actually talked through, not just a narrow set of pre-baked choices offered up front. If
  told this is moving too fast, that means slow down and reopen the discussion, not offer a
  smaller menu to pick from. Nothing gets built just because it's the literal minimum the spec
  asks for; we run decisions through the rubric in section 5 first.
- Concepts and reasoning get explained along the way, like a teacher would — not just delivered as
  a build log of finished code. The goal is understanding what's being built and why, not just
  receiving the result.
- We build one phase at a time, not everything in one pass. Each phase gets real time spent on
  ideation and architecture before any code is written for it — a deliberately-reasoned design,
  not the fastest thing that satisfies the literal spec — and gets explicitly confirmed as agreed
  before implementation starts.
- Git commit messages: short, natural language, 5–6 words, imperative (e.g. "add ec2 uptime
  handler", "wire up cost explorer caching"). No commit body/description unless explicitly asked
  for one.
- No mention of Claude/Anthropic anywhere in git history — no co-author trailers, no attribution,
  no generated-by notices.
- Frequent, small commits — one logical change at a time, not one giant commit per phase.
- This file and `docs/learning-notes.md` get updated as we go, not retroactively at the end.
- Documentation in this project is written in plain language first, made technically precise
  second — so it's useful to whoever built it and to anyone who opens the repo later without the
  full backstory.

## 5. Decision rubric

Every architecture decision in this project gets evaluated against six things: security,
scalability, reliability, flexibility, cost optimization, and — where it genuinely earns its
keep — innovation. When a decision trades one of these off against another, that tradeoff is
written down explicitly (see section 7), not left implicit.

## 6. Tech stack (from spec)

Python + boto3 for talking to AWS. OpenAI's API decides which AWS action best answers a given
question. pytest + coverage for tests (75%+ required). ruff for linting (catching style/quality
issues). Semgrep for static security scanning of our own code. Trivy for scanning the built
container image for vulnerable software. Docker + Docker Compose to package and run the agent
alongside the audit-log service. GitHub + GitHub Actions for version control and CI/CD (the
automated build/test/deploy pipeline). Amazon ECR to store the built container image. AWS CLI +
IAM for least-privilege, read-only access. All of this runs against a dedicated, budget-capped
AWS sandbox account provided for this internship.

## 7. Architecture decisions

### 7.1 Interface

A command-line tool (CLI), not a web app or API server. Run it as `python -m agent "<question>"`
for a single question, or `python -m agent` with no arguments to start an interactive mode and ask
several questions in one session — which is how we'll run through the demo's 10 questions. No HTTP
API for now: the mission asks for "a container that answers questions," not a web service, and
skipping a network-facing API means one less thing that needs to be secured. (Built in Phase 4 —
`agent/cli.py`. An earlier draft of this doc called the module `cloudops_copilot`; the actual
package is `agent`, matching the folder name used throughout this project.)

### 7.2 Capability registry + hybrid dispatch (the core design)

**The problem this solves.** A tool with only a fixed set of hand-written AWS functions can only
answer questions we thought of in advance — ask it something we didn't anticipate, and it just
says "I don't know how to do that." The tempting fix is to let the AI write and run its own code
to answer anything. That removes the limit, but introduces a real risk — and, importantly, giving
the AI a read-only AWS role does **not** fix that risk on its own. IAM only controls what *AWS
API calls* are allowed to succeed; it has no say over what arbitrary code can do more generally —
read environment variables (which, in our container, includes API keys), make network requests to
somewhere that isn't AWS at all, or simply hang the process. None of that is an AWS action, so a
read-only AWS role never gets a chance to stop it. In short: "the AI can only read AWS data, so
letting it run its own code is safe" is a false equivalence. That reasoning only becomes true once
"letting the AI decide" stops meaning "letting it run code" and instead means "letting it pick
from a fixed menu of safe, pre-built actions."

**The design has three tiers — all three now live:**

1. **Fixed registry (the primary, fast path).** A small set of hand-written, hand-tested boto3
   functions, each one self-describing: it has a name, a plain-language description, a schema
   (a precise definition of what parameters it accepts), and the actual code that does the work.
   This registry is the one place that both the "menu of choices" we hand to the AI and the CLI's
   own help text are generated from — so adding a 6th capability later means adding one entry to
   this list, not editing five different files. The five operations chosen for v1 are in 7.3.

   **How routing actually works (built Phase 4, `agent/router.py`).** Exactly one OpenAI API call
   per question, using OpenAI's native tool-calling: every registry entry becomes a callable "tool"
   (its `name`/`description`/`params_schema` map directly onto the tool schema the API expects).
   The model's response is either one or more tool calls — a single response can name several
   independent calls at once (Session 3; e.g. "what EC2 instances and VPCs do I have" needs two —
   see 7.7 for the exact scope of this), each run and formatted through the same path a lone call
   always used — or plain text (no match at all — the suggestion-only tier, below). Formatting a
   registry hit into a readable answer happens in our own deterministic code, never a second LLM
   call — so a registry-path answer always costs exactly one OpenAI request regardless of how many
   AWS calls it triggers, and its wording never varies between runs, preserving the "fast,
   thoroughly-tested" property this design was chosen for in the first place.

2. **Structured dynamic dispatch (the fallback path, for the long tail) — built (`agent/dispatch.py`).**
   When a question doesn't
   match anything in the registry, instead of writing code, the AI returns three plain values: a
   `service` (e.g. "ec2"), an `operation` (the exact name of a real boto3 method, e.g.
   `describe_instances`), and `params` (the arguments for that call). Our own code — never
   anything the AI wrote — is what actually runs `getattr(boto3_client, operation)(**params)`,
   i.e. looks up that named method on the real AWS SDK and calls it. Several checks run before
   that call is allowed to happen:
   - The operation must actually exist on that AWS client (a basic existence check).
   - The operation must appear on a **curated, one-by-one allow-list** — not just "anything
     starting with `Describe` or `List`." A prefix pattern isn't safe by itself: for example,
     `ec2:DescribeInstanceAttribute` with `Attribute=userData` is technically a "Describe" call,
     but EC2 "user data" often contains setup scripts with embedded passwords or tokens.
     CloudWatch Logs' `GetLogEvents` matches a "Get" prefix, but returns raw application log
     text, which regularly contains personal data or leaked credentials. AWS Systems Manager's
     `GetParameter`, on a parameter that isn't marked as an encrypted secret, can still return
     something like a database connection string someone stored in plain text. So the allow-list
     is built operation-by-operation per AWS service, with an explicit deny-list for exactly
     these "looks safe by name, isn't actually safe" cases (reading secrets from Secrets Manager,
     decrypting anything via KMS, decrypting SSM's "SecureString" parameters, reading EC2's
     Windows admin password, reading raw log contents, reading actual file/object contents from
     S3).
   - The parameters are checked against AWS's own definition of what that operation expects
     (botocore, the library boto3 is built on, publishes this), so a malformed request is
     rejected with a clear error instead of causing a confusing crash.
   - Results that come back in multiple pages are automatically fetched up to a cap (`MAX_ITEMS =
     200`), and if that cap is hit, the answer says so explicitly ("showing a partial, truncated
     result") — it never silently reports a partial result as if it were complete. Pagination is
     followed and merged the same way the fixed registry's own `_paginated` helper does — one
     logical call, however many pages it actually took.
   - v1's allow-list is one AWS operation per question — a question needing several different
     calls chained together isn't handled by dispatch; see 7.7 for why that's a named, deliberate
     scope limit rather than an accident.
   - Every dispatch-path call is recorded in the audit log tagged `"fallback"`, separately from
     registry-path calls — including calls the deny-list or allow-list check itself *blocked*,
     which are audited as a failed call rather than silently dropped. "Every action visible"
     includes actions our own safety net stopped, the same way an `AccessDenied` from AWS itself
     would be visible.

   **Built (`agent/dispatch.py`).** The starting allow-list slice: CloudWatch metrics
   (`ListMetrics`, `GetMetricStatistics`, `GetMetricData`), EC2 extras (`DescribeVolumes`,
   `DescribeSnapshots`, `DescribeVpcs`, `DescribeAddresses`, `DescribeRegions`), RDS
   (`DescribeDBInstances`, `DescribeDBSnapshots`, `DescribeDBClusters`), ELBv2
   (`DescribeLoadBalancers`, `DescribeTargetGroups`, `DescribeTargetHealth`), and Auto Scaling
   (`DescribeAutoScalingGroups`) — each individually verified against botocore's service models
   (the operation exists, its params and output shape are what we expect, no hidden sensitive
   field) before being added, never approved by naming pattern alone. The deny-list grew two new
   entries found the same way: Lambda's `GetFunction` (`Code.Location` is a presigned download URL
   for the function's actual source code) and SQS's `ReceiveMessage` (not actually read-only — it
   hides a message from other consumers via its visibility timeout, a real side effect even though
   nothing is deleted).

   **Session 3 additions (2026-08-06): `s3:ListBucket` and a count-only `logs:FilterLogEvents`.**
   Two more operations were added, each after the same individual-verification process as the
   original slice, and each paired with its own extra safety mechanism beyond the plain allow-list
   check:
   - `s3:ListBucket` — the boto3 method is `list_objects_v2`; the IAM action name doesn't match it,
     another instance of the same lesson ELBv2's `elasticloadbalancing:` prefix already taught.
     Returns object keys/filenames within a bucket, never their contents (`s3:GetObject` stays
     denied, unaffected). The real residual risk is a filename *encoding* something sensitive
     (`customer-ssn-export.csv`) — every returned key is checked against a small suspicious-
     substring list (`secret`, `password`, `credential`, `backup`, etc.) and flagged (`_flagged:
     true`) rather than hidden, naming the risk instead of silently suppressing it.
   - `logs:FilterLogEvents` moved off the deny-list, but only in a heavily restricted form:
     `_count_only` strips its response down to a bare `event_count` before it's ever returned — the
     actual `message` field (raw log content) never reaches the formatter, the audit preview, or the
     model. A general "redact secrets from arbitrary log text" mechanism was considered and rejected
     — regex/keyword secret detection only catches known-shaped secrets and has no bound on false
     negatives for anything else (PII, stack traces, unusual formats); building that and calling it
     "safe" would put a false guarantee at the center of a project whose safety case rests on
     permissions being genuinely, structurally read-only. Count-only sidesteps the problem rather
     than attempting to solve it: "how many ERROR lines in the last hour" is answerable without ever
     exposing what any of them said.
   - `logs:DescribeLogGroups` (group names/retention/size — metadata only) was added as a normal,
     unrestricted entry, the same shape as any other Describe-style operation already on the list.

   **How results get shaped for ranking/extreme questions.** A question like "which RDS instance
   is newest?" needs more than "find the right AWS call" — it needs picking out *one* result from
   several by some field. Rather than have the AI do that arithmetic itself (error-prone, and hard
   to verify), any tool call — registry or dispatch — can also carry `sort_by`, `sort_descending`,
   and `limit`; our own code (`router.py`'s `_shape()`) does the actual sort/slice, deterministically,
   on whichever field the model names. This is generic and applies uniformly to both tiers via a
   shared `_find_list_field()` helper, rather than being a dispatch-only feature. Genuine
   *multi-call* aggregation (e.g. "which S3 bucket is biggest?" — sizes aren't in `ListBuckets` at
   all) is a different, harder problem and is explicitly out of scope for v1 — see 7.7.

3. **Suggestion-only fallback — a permanent third tier, not a placeholder for dynamic dispatch.**
   For a question that matches neither the registry nor the dispatch allow-list, no AWS call is
   made at all. For questions that genuinely look like AWS-operations questions, the AI responds
   with a plain-language suggestion of where the user might look — general knowledge, not
   anything read from the live account, since this path never touches the account. It declines to
   engage with anything that doesn't read as a legitimate AWS-operations question.

   This is a genuinely different kind of safety guarantee than everywhere else in this design.
   Everywhere else, safety comes from IAM and our own code deciding what's allowed — never the
   AI's own judgment. Here, the AI's judgment about what's an appropriate suggestion *is* the only
   guardrail. That's an acceptable trade specifically because there is no real account data
   reachable on this path for it to get wrong — the worst case is generic bad advice, not a
   leaked value from this account. **Decided (Session 2): this stays a permanent third tier**, not
   retired now that dynamic dispatch exists — dispatch's own curated allow-list will always have
   edges it doesn't cover (either by scope, like the multi-call limit in 7.7, or by not yet having
   reviewed a given operation), and a genuinely unmatched question should get a helpful pointer
   instead of either a forced, wrong tool call or silence.

4. **IAM is the final backstop, regardless of which path answered the question.** The policy in
   `iam/policy.json` now covers both the fixed registry's actions and the dispatch allow-list's
   actions, as two separate statements — see 7.4. The drift-check test (`tests/test_iam_drift.py`)
   compares the policy against the union of both, so the two can never silently drift apart.

**Why this design over the two extremes.** A registry-only design is fully testable and easy to
audit, but rejects anything outside the list — a poor experience for something meant to hold a
conversation. A pure "let the AI figure out and run anything" design maximizes what it can answer,
but breaks two of the actual deliverables: the IAM role can't be scoped to "exactly the needed
actions" if we don't know in advance what actions might run, and the 75% test-coverage
requirement becomes close to meaningless if most of what's covered is a generic execution harness
rather than real, specific behavior. The hybrid gets fast, thoroughly-tested, nicely-worded answers
for common questions, real API access to a wider curated set of operations for everything else,
and a general-knowledge suggestion as the true catch-all — without ever running AI-written code.

### 7.3 Fixed registry operations (v1)

**Currently answerable against this project's real sandbox account, as of 2026-08-07** — a live
snapshot, cross-referenced against `iam/granted-actions.json`; kept up to date whenever that file
changes:
- ✅ EC2 instances (rich detail, state-filterable), RDS instances, RDS clusters, security groups
  open to the internet (registry)
- ✅ VPCs, subnets, route tables, internet gateways, NAT gateways, network ACLs, EKS clusters, S3
  object listing and bucket-policy public-status checks within a named bucket (dispatch)
- ❌ Everything else this project has built — S3 public-access check and IAM users/groups/roles
  (registry); Cost Explorer (registry); CloudWatch metrics, EC2 volumes/snapshots/addresses/
  regions, RDS snapshots, ELBv2, Auto Scaling, CloudWatch Logs, and ECR repository listing
  (dispatch) — all correctly decline with a specific "insufficient access" message and a
  suggestion (section 8), rather than failing or answering incorrectly. Nothing here is a bug;
  it's what the real account's role and permissions boundary actually allow today.

1. **EC2** — list instances (running by default; `state="stopped"` or `"all"` broadens it) with
   full detail per instance: type, AMI, public/private IP, VPC, availability zone, security
   groups, attached volumes, name tag, and uptime; takes an optional `min_uptime_hours` (applies
   only to running instances) to directly answer "what's been running more than 24 hours?" (this
   is the only registry entry with a real parameter — see the Phase 4 follow-up note below for why
   it didn't originally have one, and what broke without it).
2. **Security groups** — list security groups and flag any rule that allows inbound traffic from
   anywhere on the internet (`0.0.0.0/0`).
3. **S3** — list storage buckets and report whether each one blocks public access.
4. **IAM** — list existing roles, users, *and groups* (useful for spotting stale or
   overly-permissioned identities).
5. **Cost Explorer** — cost broken down by AWS service, for the current month so far.
6. **RDS** — list instances and clusters with full detail: engine/version, status, instance class,
   allocated storage, encryption, Multi-AZ, backup retention, and endpoint. Promoted from a raw
   dispatch dump to hand-formatted registry entries once real usage showed these were common
   enough to deserve real formatting (`agent/registry.py`'s `rds_instances`/`rds_clusters`).

EKS cluster listing (name, status, version, endpoint) was added to the curated dispatch
allow-list the same round — it didn't need a hand-written formatter to be genuinely useful, so it
stayed in dispatch rather than being promoted to the registry. CloudWatch (metrics like CPU usage)
remains the natural next capability to add — the whole point of the registry design is that adding
one later stays cheap.

**Pagination.** Most of the AWS calls above (`DescribeInstances`, `DescribeSecurityGroups`,
`ListBuckets`, `ListRoles`/`ListUsers`, `DescribeDBInstances`/`DescribeDBClusters`) can return
partial results with a continuation token once an account has enough resources. All of them are
fetched via boto3's built-in paginator and merged into one result (`agent/registry.py`'s
`_paginated` helper), so a larger account never silently gets an incomplete answer — the same
principle 7.2 already applies to the (deferred) fallback path, just as true for the fixed
registry. `GetCostAndUsage` (Cost Explorer) has no boto3-native paginator; it's handled with a
hand-rolled loop following its own `NextPageToken` field instead.

**Real-usage follow-up (found once we actually ran this against a live account, 2026-08-04):**
Three genuine gaps surfaced only by actually asking the CLI questions, not by unit tests:

- **EC2 uptime had no threshold filter at all.** It always returned every running instance's
  uptime, unfiltered — the "more than 24 hours" framing (the mission's own flagship example
  question) was never actually implemented as a filter anywhere, in the handler or the formatter.
  Worse: because the tool's contract didn't match "more than N hours" questions, the *same*
  question sometimes got routed to the tool and sometimes didn't — a real non-determinism, not a
  fluke. Fixed by adding a genuine `min_uptime_hours` parameter (registry.py's first real
  parameter — every other entry still has an empty `params_schema`), so the tool's contract
  actually matches what gets asked of it.
- **IAM coverage silently excluded groups.** A "how many IAM users and groups exist?" question
  routed to `iam_roles_and_users` (the closest available tool) and just said nothing about
  groups — no signal anywhere that part of the question went unanswered. Fixed by adding `list_groups`
  and renaming the capability to `iam_roles_users_and_groups` (see 7.4 for the matching policy
  update).
- **A general instance of the same failure mode.** Both of the above are the same underlying
  problem: a tool that only partially matches a question gets called anyway, and nothing ever
  says so. `agent/router.py`'s `SYSTEM_PROMPT` now explicitly tells the model not to force a
  partial match — treat a question as unmatched (and explain what's missing) rather than silently
  answering only the part a tool covers. This has to be a *routing* instruction (call vs. don't
  call), not a "disclose the gap after calling" instruction, because of the one-call design: once
  a tool is called, its formatted result *is* the final answer — the model never gets to add
  commentary afterward.

All five formatters were also rewritten for consistent, count-first, one-per-line presentation —
the original IAM formatter in particular just comma-joined every role name into one unreadable
line and never stated a count, even when the question explicitly asked "how many."

### 7.4 IAM policy — hand-written, never auto-generated or auto-applied

The IAM policy is treated as a **security artifact**, the same way we'd treat a password: it is
written by a person, reviewed in its own pull request like any other security-sensitive change,
and it is never generated by a script and never applied to the real AWS account automatically by
the CI/CD pipeline. The pipeline only ever *uses* the resulting role's credentials while it runs;
it never creates, edits, or re-applies the IAM role itself. Actually attaching this policy to the
real role in AWS is a manual, deliberate step — the exact `aws iam create-role` /
`put-role-policy` commands will be written down as a short runbook, and a person runs them.

The one part of this that *is* automated is a check, not a generator: a **drift-check test**,
included in the normal test suite, that reads both the IAM policy file and the software's own
allow-list config for the fallback path, and confirms they agree — every action the fallback
would ever try to use has a matching "Allow" entry in the policy, and the policy doesn't grant
anything wider than what the allow-list documents. If someone updates one file and forgets the
other, this test fails the build, forcing a person to go reconcile the two and get it reviewed —
it never edits AWS or the policy file on its own.

**Phase 2 scoping decision.** The drift-check doesn't need to wait for Phase 3b's finalized
fallback allow-list to exist before it can do useful work. The five fixed registry functions are
already self-describing (service + operation baked into each one, per 7.2), so that's a concrete,
enumerable list of AWS calls right now — not a stub. So Phase 2's IAM policy is scoped to exactly
that: the fixed registry's operations, nothing for the fallback path yet. The Phase 2 drift-check
test compares the policy against that same registry-derived list. This means v1's policy
deliberately did not yet cover the fallback path — that was intentional scoping, not an oversight,
and it's actually the more defensible reading of least-privilege: permissions should track what's
running, not what's planned. **Session 2: now that dynamic dispatch is built, both the policy and
the drift-check test have been extended to cover it too** — see 7.2 and 7.4's opening paragraph.

**Registry code arrives a bit early, on purpose.** The five registry functions don't exist as code
yet at the start of Phase 2 — only as the description in 7.3. Rather than write the IAM policy
against that description, Phase 2 introduces minimal, real, working versions of the five registry
entries (correct service, operation, and parameters — no shortcuts on accuracy) specifically so the
policy and drift-check test are reviewed against actual behavior, not a description of intended
behavior. The drift-check test imports this real registry module directly; there's no separate
constants file standing in for it. Full test coverage and the moto-based test suite for these
functions are still Phase 3's job — Phase 2 only needs the code to be real, not fully tested.

### 7.5 Audit log

**Decided now — what gets recorded.** The path tag is one of three values, not two:
`"registry"` (a fixed-registry capability answered it, via one or more real AWS calls),
`"fallback"` (structured dynamic dispatch answered it, per 7.2 — including blocked attempts, tagged
`success=False`), or `"suggestion"` (no AWS call at all — the AI's plain-language pointer). We deliberately do
**not** store the full raw AWS response in this log — only metadata plus a short, truncated
`preview` (added Phase 5 — see below; a real field now, not just documented intent). The
reasoning: the audit log is itself a place data lives, so if we ever logged a full response and
that response happened to contain something sensitive, we'd have just made a second copy of it
that also needs protecting — undermining the whole point of being careful about what the fallback
path is allowed to read in the first place. Every registry capability is already curated to be
non-sensitive, so a truncated preview is low-risk today; worth revisiting once dynamic dispatch
exists and the range of what gets called (and previewed) widens.

**Granularity (decided Phase 4): one audit entry per real AWS call, not per question.** A single
question can trigger several distinct AWS calls — `s3_public_access` calls `ListBuckets` once,
then `GetPublicAccessBlock` once per bucket found. The mission spec says "every action visible,"
and section 7.5's own original wording ("the operation called," singular) already implied one row
per call — so each of those calls gets its own audit entry, tied back to the one question that
triggered them via a shared `question_id`. Pagination doesn't fragment this further: fetching page
2 of a paginated call is a mechanical continuation of one request our code made once, not a
separate decision each time, so a paginated fetch (however many pages it took) is still exactly one
audit entry (see `agent/registry.py`'s `_paginated` and `audited_call`).

**The interface (Phase 4) stays decoupled from the backend (Phase 5).** Registry handlers never
call the audit log directly, or even know it exists — each accepts an optional bare callback
(`audit`), reporting a small `AwsCallRecord` per call: `service`, `operation`, `params`, `success`,
`result_count`, `preview`, `duration_seconds`. `agent/router.py` is the only place that turns that
into a real `AuditEvent` and calls `AuditLog.record(...)`. This kept `registry.py` fully decoupled
from how auditing is implemented — which is exactly what let Phase 5 add the `preview` field and
build the real backend without touching any registry handler's logic.

**Built now (Phase 5): the real service, alongside the interim file-based one.**
`audit_log/app.py` is a small FastAPI service — `POST /events` to record, `GET /events` (with
optional filters: `question_id`, `path`, `service`, `success`) to review the trail, `GET /health`
for a basic liveness check. `audit_log/storage.py` backs it with SQLite (`audit_log/audit.db`,
gitignored) — a plain file-based database, not a separate database server, so the mission's
two-service requirement (agent + audit-log, no third container) still holds. `agent/audit.py`'s
`HttpAuditLog` is the agent-side client that talks to it, implementing the same `AuditLog`
interface `FileAuditLog` always has — `router.py` didn't change at all to support this.
`FileAuditLog` stays too, as a genuinely useful no-network option for quick local runs.

**Resilience: logging must never break answering.** `HttpAuditLog` uses a short timeout (2s
default) and catches any HTTP failure rather than raising — a slow or unreachable audit-log
service degrades to "this one action didn't get logged," never "the agent stopped answering
questions." Failures print to stderr rather than being silently swallowed. No retry/backoff logic
— that's more resilience engineering than this project's scale warrants.

**Deliberately deferred to a later phase, on purpose:** the actual Docker Compose wiring — both
Dockerfiles, the custom network, containerizing the agent for the first time. The service and
client were built and verified talking to each other over plain `localhost` (running the service
with `uvicorn`, no container involved) specifically so that later phase is only about containers
and networking, not debugging application logic at the same time. That's a deliberate sequencing
choice, not scope creep avoidance — see learning-notes.md.

**Docker phase, taken granularly, step by step, on purpose** (per the user's explicit ask to
understand each piece deeply, not just get a working result): step 1, the audit-log service's own
`Dockerfile`, is done and verified standalone (`docker build` + `docker run`, no Compose yet) —
pinned base image (`python:3.12.13-slim`, not the floating `3.12-slim` tag), runs as a non-root
`appuser`, and a `.dockerignore` keeps dev-only cruft (tests, `__pycache__`, stale local
`events.jsonl`, the `Dockerfile` itself) out of the built image. Two real bugs surfaced and got
fixed by actually running the container, not just building it — see learning-notes.md.

Step 2, the agent's own `Dockerfile`, is also done and verified standalone. Different shape than
the service: the agent isn't long-running, so `ENTRYPOINT ["python", "-m", "agent"]` (not `CMD`)
is what runs — anything passed after the image name in `docker run` becomes an argument to it,
mirroring the CLI's own single-question-vs-interactive branching with no extra logic needed.
Verified secrets really aren't baked in by proving the *failure* case first: running the image
with zero env vars fails with a clear missing-credentials error, rather than mysteriously working.
Ran a real end-to-end question against the live sandbox account and OpenAI, from inside the
container, via `--env-file .env` plus a mounted `~/.aws` for the SSO profile (see
learning-notes.md for the mount-permission gotcha that surfaced here).

Step 3, `docker-compose.yml`, is also done — both containers, a custom `cloudcopilot-net` network,
and verified talking to each other *by service name* (`http://audit-log:8001`), not a hardcoded
IP, satisfying that specific best-practice requirement literally, not just in spirit. `agent/cli.py`
gained a small `_default_audit_log()`: `FileAuditLog` stays the plain-local default, and setting
`AUDIT_LOG_URL` (which Compose does) switches it to `HttpAuditLog` — Compose is the only thing
that needs to know this switch exists. The agent is deliberately *not* a normal always-started
service — it's `profiles: ["cli"]`'d out of a bare `docker compose up`, since it's a one-shot CLI
tool, not a daemon; `docker compose run --rm agent "<question>"` is how you actually use it.
`audit_log/storage.py`'s DB path became configurable (`AUDIT_LOG_DB_PATH`) specifically so a named
volume could be mounted at a directory separate from the code (`/data`, not `/app/audit_log`) —
verified events genuinely survive a full `docker compose down` + `up` cycle. Found one real, subtle
bug along the way that had nothing to do with any of this: an AWS-call failure wasn't showing up
in the audit trail at all, which traced back to simply forgetting to rebuild the agent image after
editing `cli.py` — the running container was still executing old code. See learning-notes.md.
A from-scratch, deeply-detailed reference of every Docker concept touched across all three steps
was also written, kept local-only (not committed) at the user's request, purely for their own
learning reference.

**A read-only dashboard (added Session 3, 2026-08-06) — `GET /dashboard` on the audit-log service
itself, no new service, no new port.** Not a mission requirement; added specifically to make "every
action visible" something a reviewer can actually see at a glance instead of reading raw JSON rows.
Backed by a new `GET /stats` endpoint (`EventStore.stats()`) — aggregates (totals, success/failure
split, breakdown by path and by service) computed in SQL, not shipped as raw rows for the browser to
tally, the same "our own code does the deterministic work" principle already applied to the agent's
sort/limit shaping. Both `/stats` and `/dashboard` accept the same `path`/`service`/`success`
filters as `/events`, so a filtered dashboard view and the table underneath it always agree.
`dashboard.html` is a single self-contained page (inline CSS/JS, no build step, no external
requests — consistent with this project's "no new dependencies for a demo feature" instinct) built
against a validated categorical/sequential/status color system (registry/fallback/suggestion get
fixed, non-cycled hues; AWS-service-by-volume gets a single sequential hue; success/failure gets the
reserved status palette, never color alone — always paired with a dot + label). Verified visually,
not just via the API responses: headless-screenshotted both light and dark mode against the real
running container after rebuilding it, confirming legible contrast and no layout collisions in
either mode — the same "render it and look at it" discipline as every other verified fix this
project has made, applied to a UI instead of a CLI answer for the first time.
### 7.6 CI/CD → pushing images to ECR

**Built (Session 2, 2026-08-05): `.github/workflows/CloudCopilot-ci.yml`.** Four jobs, triggered
only on changes under `CloudCopilot/**`:

```
lint ──▶ test    ──┐
     └─▶ semgrep ──┴──▶ build-scan-push
```

1. **`lint`** — `ruff check` + `ruff format --check`, against an explicit rule selection in
   `pyproject.toml` (not ruff's bare defaults — see `docs/ci-cd-concepts.md` for why that matters).
2. **`test`** — `pytest` across both `tests/` and `audit_log/tests/`, gated by
   `--cov-fail-under=75`. Verified this gate genuinely fails below threshold, not just that it
   passes today (99.79% actual coverage).
3. **`semgrep`** (Session 2 optimization) — scans source (`agent/`, `audit_log/`) for risky code
   patterns. Split into its own job, `needs: lint` same as `test`, so it runs *alongside* `test`
   rather than after it — it was originally bundled inside `build-scan-push`, but it never actually
   needed the built Docker images to exist first; it only ever scanned source. Moving it earlier
   shortens the pipeline's critical path with no change in what gets checked.
4. **`build-scan-push`** (`needs: [test, semgrep]`) — builds both Docker images, runs Trivy against
   both (`--severity HIGH,CRITICAL --ignore-unfixed`, `--exit-code 1`), then — only when running on
   `main` — pushes both images to ECR. Considered splitting this further into fully independent
   `build`/`scan`/`push` jobs too, for the same per-stage visibility reason as the Semgrep move; decided
   against it for now — jobs run on separate VMs, so images would need to be handed off between them via
   `docker save` + artifact upload/download, adding real transfer overhead for a granularity benefit that
   didn't seem worth it yet. Revisit if the pipeline grows more stages that would benefit from it.

**Authentication — RESOLVED (2026-08-06): OIDC role assumption is live, the static-credential
downgrade is retired.** A dedicated role, `intern-shruti-ci-role`, was provisioned specifically for
this pipeline (separate from the agent's own read-only role, per an explicit decision not to reuse
one role for both) and wired into the workflow's `Configure AWS credentials` step via
`role-to-assume: ${{ vars.AWS_ECR_PUSH_ROLE_ARN }}`. The ARN itself is a GitHub repo Variable, not
hardcoded in the committed workflow — same "keep the literal ARN out of every committed file"
convention already applied to the agent's own role (section 8) — set via `gh variable set`. GitHub
now issues a short-lived identity token per run, which AWS exchanges for temporary credentials; no
long-lived AWS access key is stored as a GitHub secret anywhere. The three temporary SSO-session
secrets (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`) are no longer referenced
by the workflow, left in place for now pending a decision on cleanup.

**How the role actually got created — worth recording, since it changes what "blocked on
`iam:CreateRole`" turned out to mean.** The sandbox identity can create IAM roles via the CLI, but
only when a `--permissions-boundary` argument is supplied (a policy, provisioned by whoever
administers the sandbox, capping the role's effective permissions regardless of what its own
identity-based policy grants) — the AWS console's role-creation UI doesn't support specifying this
in the same flow, which is why creating it there returned `AccessDenied`. Not a hard, no-workaround
block after all, just a narrower path than section 8 originally described. `ci/oidc-trust-policy.json`
(trust: this repo, `ref:refs/heads/main` only) and `ci/ecr-push-policy.json` (permissions: push to
the two real ECR repos, corrected below) were filled in and used as-is via `create-role` +
`put-role-policy` — no drift between what was designed and what actually got attached, since the
same hand-authored files were the ones used.

The role's trust policy scopes assumption to this exact repo and `ref:refs/heads/main` only —
deliberately duplicating the workflow's own `if: github.ref == 'refs/heads/main'` gate at the AWS
side too, so a future edit to the workflow couldn't silently widen who can push images even by
mistake. That property now actually holds, not just as a documented intent for later.

**A real drift bug found and fixed while wiring this up:** `ci/ecr-push-policy.json` still had
placeholder ECR repo names (`cloudcopilot-agent`/`cloudcopilot-audit-log`) that never matched the
two repositories actually created (see below) or what the workflow's own push step already used.
Corrected to the real names before the policy was attached to the role.

**ECR repository names (added Session 2, 2026-08-05):** two repositories already created manually
in the sandbox account — `intern-shruti-cloudops-copilot-agent` and
`intern-shruti-cloudops-copilot-audit-service` — referenced directly in the push step rather than
computed from the local image name, since there's no guarantee an ECR repo's name matches the
locally-built image tag.

**Two real decisions worth recording (full reasoning in `docs/ci-cd-concepts.md`):**
- **Trivy's gate uses `--ignore-unfixed`.** Every one of the base image's current HIGH/CRITICAL
  findings (23, all inherited from `python:3.12.13-slim`'s Debian packages, none caused by this
  project's own code) has zero available fix upstream right now. Gating on *any* HIGH/CRITICAL
  literally would make the build permanently fail with nothing actionable to do about it.
  `--ignore-unfixed` (Trivy's own recommended practice for this exact situation) keeps the gate
  meaningful — it still fails on anything actually fixable — without being blocked forever by
  unpatched OS packages outside this project's control.
- **A Semgrep finding in `audit_log/storage.py` was a verified false positive, not suppressed
  blindly.** The rule targets SQLAlchemy specifically (this code uses plain `sqlite3`), and the
  only f-string-interpolated part of the query is built from hardcoded literals, never
  caller-controlled text — every actual value is still safely parameterized. Suppressed with an
  inline comment explaining exactly why, so the reasoning survives in the code itself.

**Status: verified end-to-end against a real GitHub Actions run (2026-08-06), not just correct by
construction.** The push to `main` that landed this wiring auto-triggered the workflow (it touches
paths under `CloudCopilot/**`); the run authenticated as `assumedRoleId
AROA3FLD5HU3GOCAXVOSR:GitHubActions` via OIDC, logged into ECR, and pushed both images successfully
— confirmed directly with `aws ecr describe-images` against both real repositories afterward, not
just a green checkmark. Everything else in the pipeline — lint, tests, both Docker builds, Semgrep,
and both Trivy scans — was already verified locally end-to-end using `act` before any of this.

### 7.7 Known scope limits for v1 (documented on purpose, not accidental gaps)

- **One AWS region at a time.** Most of what we're querying (EC2, RDS, etc.) is specific to a
  single AWS region, and v1 only looks at one configured default region. Supporting multiple
  regions at once is a reasonable future improvement, not something blocking this design.
- **Clear failure reasons.** A question outside the registry and the dispatch allow-list never
  reaches AWS at all — it gets the suggestion-only fallback response instead of a raw error.
  Within dispatch, failure reasons are: our own deny-list or allow-list check blocked it before it
  ever reached AWS (audited as a failed call, per 7.2); AWS itself refused it ("AccessDenied" —
  this actually means the safety net is working correctly, not that something is broken); AWS had
  a real error or was rate-limiting us; or the AI referenced a service/operation that doesn't exist
  at all (rejected by `run_curated_call` the same as any other not-on-the-allow-list case). Either
  way, the user never sees a raw error message.
- **Multiple *independent* calls per question are supported (Session 3); genuine dependent fan-out
  is still deferred.** `answer_question` now processes every tool call the model returns in its one
  response (capped at `MAX_CALLS_PER_QUESTION = 5`), not just the first — so "what EC2 instances and
  VPCs do I have" correctly triggers both calls and combines both real answers. This covers
  compound questions the model can fully decide on upfront, in one turn — it does **not** cover
  dependent fan-out, where the model would need to *see* one call's result before deciding on
  follow-up calls (e.g. "which S3 bucket is biggest?" — bucket sizes aren't in `ListBuckets`; that
  needs a separate call per bucket *after* seeing the bucket list, then a comparison). That's a
  genuine multi-turn agentic loop, a materially bigger and costlier change than the one-turn,
  decide-everything-upfront case built here — and moot regardless right now, since
  `s3:ListAllMyBuckets` (the discovery step) is itself blocked by the permissions boundary (section
  8), so there'd be nothing to enumerate even with the loop built. Deliberately deferred, not
  silently unsupported. The generic `sort_by`/`sort_descending`/`limit` shaping (7.2) still solves
  the single-call version of a ranking question ("which is the newest of these" when they all come
  back from one call already).

## 8. Open / not yet decided

- **How the running agent authenticates to AWS — RESOLVED (2026-08-06).** A read-only role ARN was
  provided by whoever administers the sandbox account — the assume-via-STS path `iam/runbook.md` was
  always written for, just with an externally-provisioned role instead of one we create ourselves
  (steps 3–4 of that runbook, creating the role and attaching `iam/policy.json`, don't apply here;
  the role already exists). Wired up as a *second*, role-assuming AWS profile chained on top of the
  existing SSO one (`role_arn` + `source_profile` in `~/.aws/config`, per `docs/local-setup.md`
  section 3) — `.env`'s `AWS_PROFILE` now points at this new profile
  (`cloudcopilot-agent`), so both the AWS CLI and boto3 transparently assume the role and re-assume
  it automatically whenever the underlying SSO session refreshes; no manual credential export, ever.
  Verified for real, end to end, not just that `sts:assume-role` succeeds: ran the actual agent
  against the live sandbox account through this profile for both a granted capability (security
  groups open to the internet — real results returned) and a capability the role's own scope-mismatch
  blocks (Cost Explorer — declined cleanly with the exact missing-action message, followed by a real
  LLM-generated suggestion). The previous temporary broader-credential workaround is retired;
  `iam/runbook.md` now flags at the top that this project's real role wasn't created via its own
  steps 1–4, pointing to the externally-provided-role path instead.

  **The role's attached policy doesn't match what our code was designed against — discovered by
  comparing the two directly, not assumed to align just because both are called "read-only."**
  Roughly 30% overlaps with what `iam/policy.json` (our own hand-authored, aspirational ask)
  describes; the rest is a genuinely different set — some of it narrower (missing
  `s3:GetBucketPublicAccessBlock`, `iam:ListUsers`, `iam:ListGroups`, and all of `ce:GetCostAndUsage`
  — that last one breaks an entire registry capability and is the mission spec's own named example),
  some of it wider (EC2 networking metadata, EKS, and the two new dispatch capabilities added in
  7.2's Session 3 note). **Not currently negotiable** — confirmed directly, not assumed — so the
  missing pieces are handled in code rather than by requesting a policy change:
  - **`iam/granted-actions.json`** is a new, separate, hand-maintained file recording what this
    specific role actually grants — deliberately *not* the same document as `iam/policy.json`.
    `policy.json` stays exactly what it always was: our own ideal least-privilege ask, kept in
    perfect sync with the registry/dispatch code's needs via the existing drift-check
    (`tests/test_iam_drift.py`, unchanged). `granted-actions.json` describes an external fact
    instead — what's really enforced right now — and is used only by the new permission gate below.
  - **`agent/permissions.py`'s `missing_actions()`** checks a matched capability's required IAM
    action(s) against `granted-actions.json` *before* any AWS call is attempted, wired into
    `agent/router.py` for both the registry and dispatch tiers. A capability that needs something
    ungranted is declined immediately with a specific message (e.g. "needs `ce:GetCostAndUsage`,
    which isn't part of this agent's currently granted AWS role"), rather than discovered via a
    generic `AccessDenied` after the fact — deterministic, and no wasted AWS call. This is checked
    proactively, but AWS's own `AccessDenied` is still the backstop for anything this static file
    didn't anticipate.
  - Per this project's existing "a blocked action is still an action" precedent (section 7.5, and
    the dispatch deny-list's own audit behavior), a permission-gap decline is still recorded in the
    audit log — tagged under whichever tier matched (`"registry"`/`"fallback"`), `success=False`,
    with the missing action(s) named in `preview`.
  - Immediately after declining, the agent asks the model for one short, general, plain-language
    suggestion of where else to look (reusing the suggestion tier's own non-AWS-connected mechanic)
    — a deliberate, narrow exception to the "exactly one OpenAI call per question" rule (7.2), made
    only for this specific case.
  - `tests/test_permissions.py` includes a regression test locking in the *exact current* gap
    (which actions are missing today) against the real `granted-actions.json` + the real
    registry/dispatch action sets — it's meant to fail the moment either side changes, forcing a
    conscious update rather than a silent drift between what's documented and what's actually true.
    Tests that exercise a capability's own correctness (not this deployment constraint) pass their
    own "everything granted" override to `answer_question`, the same dependency-injection seam
    already used for `session`/`openai_client`.

  **Retired (2026-08-06):** the previous temporary measure — running the agent directly under the
  sandbox identity's own broader-than-read-only credentials while this blocker was open — is no
  longer in use now that the role above is wired up and verified working. That workaround was
  accepted at the time specifically because it was bounded, not because the read-only requirement
  stopped mattering: every AWS-calling code path in this project (the fixed registry, and dynamic
  dispatch's curated allow-list, 7.2) only ever calls individually hand-verified `Describe`/`List`/
  `Get` operations, so even with broader credentials behind it there was never a code path capable
  of a write or destructive call. Kept here as a record of that reasoning, not as current behavior
  — the mission's explicit requirement ("read-only IAM role... reviewed before use") is now actually
  satisfied, not just bounded around.
- **Setting up ECR + GitHub OIDC for the CI/CD pipeline's push stage — RESOLVED (2026-08-06).** A
  separate, dedicated role (`intern-shruti-ci-role`) from the agent's own read-only one, wired into
  the workflow via OIDC and confirmed by a real, successful GitHub Actions run — both images
  verified present in ECR afterward. See 7.6 for the full story.
- Whether Cost Explorer results get cached, and for how long — worth deciding because Cost
  Explorer charges a small fee per API call, so repeated identical questions should ideally not
  re-trigger it.
- Growing dynamic dispatch's allow-list further (more services/operations) as real usage surfaces
  more gaps — the same one-by-one, hand-verified process used for the starting slice (7.2), not a
  one-time-only exercise.
- Genuine multi-call aggregation questions (7.7) — still an open design question for whenever it's
  prioritized, not just "not built yet."

Resolved this phase (Phase 4): how the CLI/LLM-routing layer decides "registry match" vs.
"suggestion-only fallback" (OpenAI native tool-calling, one call per question — see 7.2); whether
the suggestion-only fallback logs the question (yes, tagged `"suggestion"` — see 7.5); the exact
wording of the suggestion tier's guardrail (`agent/router.py`'s `SYSTEM_PROMPT`).

Resolved this session (Session 2): dynamic dispatch is built, tested, and IAM-covered (7.2, 7.4);
the suggestion-only fallback is decided to be a permanent third tier, not a placeholder retired
once dispatch existed (7.2 item 3); how ranking/extreme questions ("newest", "biggest", "top 3")
get answered — a generic `sort_by`/`sort_descending`/`limit` shaping layer, applied uniformly to
both registry and dispatch results (7.2); genuine multi-call aggregation is explicitly scoped out
of v1, not silently unsupported (7.7).

## 9. Change log

- 2026-08-03 — Initial architecture session. Decided: hybrid capability-registry + structured
  dynamic-dispatch design, hand-written IAM policy with an automated drift-check test (no
  auto-generation or auto-apply), CLI interface, the five fixed operations listed in 7.3, OIDC for
  ECR pushes, and the audit log's recorded fields (backend implementation deferred).
- 2026-08-03 — Phase 2 kickoff. Decided: Phase 2's IAM policy and drift-check test scope to the
  fixed registry's operations only, since those are already enumerable from the self-describing
  registry entries; fallback-path coverage is deliberately deferred to Phase 3b, added to both the
  policy and the test in that phase's own PR once the allow-list is finalized.
- 2026-08-03 — Since the registry didn't exist as code yet, decided to build minimal, real (not
  stubbed-out/fake) versions of the five registry functions as part of Phase 2, before writing the
  policy — so the policy and drift-check are reviewed against real behavior. Full unit tests for
  these functions remain Phase 3's responsibility.
- 2026-08-03 — Phase 2 IAM policy and drift-check test written (`iam/policy.json`,
  `tests/test_iam_drift.py`), scoped to the seven distinct AWS actions the five registry functions
  use: `ec2:DescribeInstances`, `ec2:DescribeSecurityGroups`, `s3:ListAllMyBuckets`,
  `s3:GetBucketPublicAccessBlock`, `iam:ListRoles`, `iam:ListUsers`, `ce:GetCostAndUsage`. Verified
  against AWS's Service Authorization Reference, not inferred from the boto3 method names — see
  learning-notes.md for why that distinction mattered here. Still open: the runbook for actually
  attaching this policy depends on deciding how the running agent authenticates to AWS in the
  first place (see section 8).
- 2026-08-03 — Decided to defer structured dynamic dispatch until after a working base version
  ships. It was never a mission-spec requirement (the spec's bar is "5+ operations" and "10
  questions answered," both satisfiable by the fixed registry alone) — it was a Session 1
  ambition, not abandoned, just resequenced. v1's actual behavior for anything outside the
  registry: no AWS call at all, just a plain-language, non-sensitive suggestion from the AI about
  where to look, only for genuine AWS-operations questions. Side effect: the IAM policy stays
  exactly at the fixed registry's 7 actions until dynamic dispatch is actually built — no
  speculative widening. See 7.2 tier 3 and section 8 for what's still open about this.
- 2026-08-04 — Phase 3. Found and fixed a real gap: none of the five registry functions handled
  AWS pagination, so a large enough account could get a silently-incomplete answer. Added
  pagination to all five (see 7.3). Then wrote the full unit test suite (`tests/test_registry.py`)
  — moto for EC2/S3/IAM (which track real create/list state), `botocore.stub.Stubber` for Cost
  Explorer (moto doesn't simulate real billing data at all). 14 tests, 100% coverage on
  `agent/registry.py`, well above the 75% requirement. Added `requirements.txt` /
  `requirements-dev.txt` since the project now has real dependencies to pin, not just
  throwaway verification installs.
- 2026-08-04 — Phase 4: built the LLM routing layer, CLI, and audit-log interface. Decided:
  one OpenAI API call per question via native tool-calling (registry entries become tools;
  formatting a hit is our own deterministic code, never a second LLM call); one audit entry per
  real AWS call rather than per question, since a single question can trigger several distinct
  calls (`agent/registry.py`'s new `audited_call` — a bare callback threaded through handlers, kept
  fully decoupled from `agent/audit.py`'s actual `AuditLog`/`AuditEvent` types); a three-way path
  tag (`registry`/`fallback`/`suggestion`); and a local JSON-lines file (`agent/audit.py`'s
  `FileAuditLog`) as the interim stand-in for Phase 5's real backend. CLI supports both a single
  question and an interactive session (`agent/cli.py`), run via `python -m agent`. 38 tests total,
  99% coverage (the one uncovered line is `agent/__main__.py`'s trivial entry-point shim).
- 2026-08-04 — Wrote `iam/runbook.md` and `iam/trust-policy.json` for the intended role-based
  auth. Attempting it revealed the current sandbox identity has neither `iam:CreateRole` nor
  `sts:AssumeRole`. Decided: for local dry-run testing only, run the agent under the sandbox
  identity's own broader credentials, explicitly time-boxed and logged as not satisfying the
  read-only requirement — bounded for now only because the fixed registry's code never issues
  anything but `Describe`/`List`/`Get` calls. Must be resolved before the real demo. See section 8.
- 2026-08-04 — First real dry run against the live sandbox account (real OpenAI + real AWS calls)
  surfaced three genuine gaps unit tests hadn't caught: EC2 uptime had no actual threshold filter
  (causing non-deterministic routing on the mission's own example question), IAM coverage silently
  excluded groups, and the IAM formatter dumped a comma-joined wall of text with no count even when
  "how many" was asked. Fixed all three (see 7.3), added a `SYSTEM_PROMPT` rule against forcing a
  partial tool match, and rewrote all five formatters for consistent, count-first output. Also
  found, while writing tests for these fixes, that this dev machine has real ambient AWS
  credentials (a corporate SSO identity, unrelated to the sandbox `.env`) — a test missing
  `@mock_aws` doesn't fail, it silently attempts a real call. Added `tests/conftest.py`, an
  autouse fixture forcing fake credentials for every test, so that class of mistake now fails
  safely instead of silently.
- 2026-08-04 — Phase 5: built the real audit-log service (`audit_log/app.py` + `storage.py`,
  FastAPI + SQLite) and the agent-side `HttpAuditLog` client, plus the `preview` field 7.5 always
  said should exist but Phase 4 never actually added. Decided: the service both records (`POST
  /events`) and can be queried (`GET /events`, with filters); logging failures are caught and
  reported, never allowed to break the agent's actual answer; adding `preview` meant restructuring
  the registry's audit callback from positional args into a small `AwsCallRecord` (cleaner at 7
  fields than more positional args would have been). Deliberately deferred: the actual Docker
  Compose wiring, kept as its own later phase — verified the service and client working together
  over plain `localhost` first, so that phase is purely about containers/networking, not debugging
  application code at the same time. 58 tests, 99% coverage across `agent/` and `audit_log/`.
- 2026-08-04 — Running the agent locally in practice surfaced that pasted raw AWS session
  credentials in `.env` expire and need re-copying repeatedly (the sandbox identity is
  SSO-federated, so this can't be avoided entirely — see section 8). Switched to an
  `AWS_PROFILE`-based setup: `.env` holds a static profile name, `aws sso login` refreshes the
  session without ever touching `.env` again, and boto3 resolves it with zero code changes. Wrote
  `docs/local-setup.md` with the full steps, separate from `iam/runbook.md` (which is about the
  intended role-based auth, not this workaround).
- 2026-08-04 — Docker phase, step 1: `audit_log/Dockerfile`, built and run standalone (no Compose
  yet, deliberately granular per the user's request to understand each piece). Pinned to
  `python:3.12.13-slim`. Two real bugs found by actually running the built container, not just
  building it: the app crashed on startup because `COPY` runs as root but the app then switches to
  a non-root user before it needs to *create* the SQLite file, so the directory wasn't writable
  (fixed with `chown` before the `USER` switch); and a `.dockerignore` was needed because, without
  one, the image picked up `__pycache__`, a stale local `events.jsonl`, and the entire `tests/`
  directory — its patterns needed an explicit `**/` prefix to match anything not at the build
  context's root (a bare `__pycache__/` only matches the top level, not `audit_log/__pycache__/`).
- 2026-08-04 — Docker phase, step 2: `agent/Dockerfile`, built and run standalone. Used
  `ENTRYPOINT` rather than `CMD` since the agent is a CLI, not a server — arguments passed to
  `docker run` become arguments to `python -m agent`, matching the CLI's own single-question vs.
  interactive branching for free. Verified end-to-end against the live sandbox account and real
  OpenAI from inside the container. Found one real gotcha doing so: mounting `~/.aws` read-only so
  boto3 could resolve the SSO profile failed with `Read-only file system` — boto3's SSO credential
  provider writes back to the cache directory when refreshing a token, not just reads it. Fixed by
  mounting read-write instead.
- 2026-08-04 — Docker phase, step 3: `docker-compose.yml`. Both containers verified reaching each
  other by service name over a custom network; the agent's audit logging now switches from
  `FileAuditLog` to `HttpAuditLog` via a new `AUDIT_LOG_URL` env var, set by Compose. Agent given
  `profiles: ["cli"]` so a bare `docker compose up` doesn't try to start a one-shot CLI tool as if
  it were a daemon. `audit_log/storage.py`'s DB path made configurable so a named volume could
  persist it without hiding the service's own code underneath (a volume mounted at the code's own
  directory would have replaced app.py/storage.py with the volume's initially-empty contents).
  Verified persistence survives a full `docker compose down` + `up`. Diagnosed a real bug — AWS
  failures weren't reaching the audit trail — down to forgetting to rebuild the agent image after
  editing `cli.py`, not a design flaw. Wrote a from-scratch Docker concepts reference, kept
  local-only per the user's request.
- 2026-08-05 — Built the CI/CD pipeline (`.github/workflows/CloudCopilot-ci.yml`): lint → test
  (coverage gate) → build both images → Semgrep → Trivy → push to ECR via OIDC, matching the
  mission spec's literal ordering. Added an explicit `pyproject.toml` ruff configuration rather
  than relying on ruff's bare defaults (~50 findings fixed, mostly auto-fixed modernizations, plus
  3 tests narrowed from asserting a blind `Exception` to the actual expected `ClientError`).
  Decided Trivy's gate needs `--ignore-unfixed` — the base image's current HIGH/CRITICAL findings
  all have zero available fix upstream, so gating on them literally would fail forever; confirmed
  by checking every finding's `FixedVersion` field, not assumed. Verified and suppressed one real
  Semgrep false positive in `audit_log/storage.py` (targets SQLAlchemy; this is plain `sqlite3`
  with genuine parameterization). Wrote the OIDC trust policy, ECR push permissions policy
  (verified exact ECR action names against botocore first), and a runbook (`ci/`) for the AWS-side
  setup — not yet applied, same `iam:CreateRole` blocker as the agent's own role (see section 8).
  Verified the entire pipeline except the AWS push step locally using `act`, including working
  around a known `act`-specific Node.js limitation by checking each job's substantive steps
  directly rather than accepting a superficial pass/fail. Wrote a from-scratch CI/CD concepts
  reference, kept local-only, same treatment as the Docker one.
- 2026-08-05 — Built structured dynamic dispatch (`agent/dispatch.py`), resolving the tier that
  was deferred back on 2026-08-03. Decided first: the suggestion-only fallback stays a permanent
  third tier even now that dispatch exists, not retired (7.2 item 3) — dispatch's own curated
  allow-list will always have edges. Starting allow-list: CloudWatch metrics, EC2 extras
  (volumes/snapshots/VPCs/addresses/regions), RDS, ELBv2, Auto Scaling — each operation
  individually verified against botocore's service models before being added, catching that
  ELBv2's boto3 client name (`elbv2`) diverges from its IAM action prefix
  (`elasticloadbalancing`), which the drift-check test would otherwise have silently gotten wrong.
  Deny-list grew two entries found the same way: Lambda's `GetFunction` (a presigned source-code
  download URL) and SQS's `ReceiveMessage` (a real side effect via visibility timeout, despite
  deleting nothing). Also built a generic `sort_by`/`sort_descending`/`limit` result-shaping layer
  (`router.py`'s `_shape()`), applied uniformly to registry and dispatch results, so ranking/extreme
  questions ("newest", "biggest", "top 3") get answered by our own deterministic sort/slice instead
  of the AI doing the arithmetic itself; scoped genuine multi-call aggregation (e.g. "biggest S3
  bucket by size") out of v1 on purpose, same treatment as the single-region limit (7.7). Widened
  `iam/policy.json` with a second statement for the dispatch allow-list and rewrote
  `tests/test_iam_drift.py` to check the union of registry + dispatch actions. Found during
  end-to-end smoke testing (real OpenAI + moto) that the model initially routed the "which RDS
  instance is newest?" question to dispatch correctly but didn't reliably apply `sort_by`/`limit`
  on its own — diagnosed as a harder compound task than the registry's explicit named parameters
  (deciding shaping is needed *and* guessing the real AWS field name, with no schema hint).
  Strengthened `SYSTEM_PROMPT` from a passive suggestion into an imperative instruction with a
  concrete worked example; re-ran the smoke test and confirmed the fix. 87 tests total.
- 2026-08-05 — CI/CD pipeline optimization pass (7.6). Split Semgrep into its own job running
  alongside `test` instead of after it inside `build-scan-push`, since it only ever scanned source
  and never actually needed the built images first — shortens the critical path with no behavior
  change. Considered also splitting `build-scan-push` itself into fully independent build/scan/push
  jobs for the same visibility reason; decided against it since jobs run on separate VMs and images
  would need `docker save` + artifact upload/download to cross that boundary — real overhead for a
  benefit that didn't seem worth it yet. Temporarily swapped the ECR push step's authentication from
  OIDC (still blocked on `iam:CreateRole`, see section 8) to the sandbox SSO identity's own
  temporary session credentials, stored as three GitHub secrets, specifically to verify the rest of
  the push logic works end-to-end against real ECR — logged directly in the workflow as explicitly
  temporary and expected to need periodic secret refreshes as the SSO session expires; the OIDC
  design remains the intended permanent mechanism and is unchanged in `ci/`. Added the two real ECR
  repository names (`intern-shruti-cloudops-copilot-agent`,
  `intern-shruti-cloudops-copilot-audit-service`), created manually in the sandbox account.
- 2026-08-06 — Session 3: a read-only role ARN was provided for the agent, resolving the
  role-assumption blocker (section 8) — but its attached policy turned out to grant a genuinely
  different set of actions than `iam/policy.json` describes, missing several things our registry
  and dispatch already use (notably all of Cost Explorer) and granting a few things we didn't have
  (EC2 networking metadata, EKS, S3 object listing, CloudWatch Logs filtering — plus ECR push
  actions we've explicitly agreed never to exercise from this role). Since the role isn't
  negotiable, built a proactive permission gate instead of reacting to `AccessDenied`:
  `iam/granted-actions.json` (a new, hand-maintained snapshot of what's actually granted, kept
  deliberately separate from `policy.json`'s aspirational ask) and `agent/permissions.py`'s
  `missing_actions()`, checked in `agent/router.py` before either tier attempts an AWS call. A
  blocked capability is declined with a specific message naming the missing action, audited as a
  blocked attempt (same "a blocked action is still visible" precedent as dispatch's deny-list), and
  followed by one extra, narrow-exception LLM call for a general suggestion. Added two new dispatch
  capabilities to make use of extra grants that were reasonably safe to add: `s3:ListBucket`
  (`list_objects_v2`) with a suspicious-object-key flag (best-effort on filenames, not a guarantee —
  `s3:GetObject` stays denied regardless), and `logs:FilterLogEvents` moved off the deny-list but
  restricted to a bare `event_count` — raw log message content never leaves `agent/dispatch.py`.
  Deliberately did *not* build a general "redact secrets from log text" mechanism — free-text
  redaction can't bound its own false-negative rate, and a false safety guarantee there would
  undermine this project's actual safety case. `logs:DescribeLogGroups` added as a normal,
  unrestricted entry. `iam/policy.json` and the existing drift-check test were updated to include
  the two new dispatch actions, unchanged in mechanism. Added `tests/test_permissions.py`,
  including a regression test that locks in today's exact permission gap so it fails loudly if
  either side ever changes silently. 99 tests, 99.38% coverage. Still open: wiring the actual role
  ARN into a local AWS profile (need the literal ARN value) and the separate ECR-pipeline role.
- 2026-08-06 — Wired up the provided read-only role ARN, resolving the authentication blocker
  (section 8). Added a second, role-assuming AWS profile (`cloudcopilot-agent`) chained on top of
  the existing SSO profile via `role_arn` + `source_profile` in `~/.aws/config`, verified with
  `aws sts assume-role` and `get-caller-identity` before trusting it, then pointed `.env`'s
  `AWS_PROFILE` at it. Verified end-to-end against the live sandbox account: a granted capability
  (security groups open to the internet) returned real results, and a capability the role's
  scope-mismatch blocks (Cost Explorer) declined cleanly with the exact missing-action message plus
  a real LLM suggestion — both tiers of the permission gate (section 8) confirmed working against
  reality, not just unit tests. Retired the temporary broader-credential workaround. Updated
  `docs/local-setup.md` (role-assuming profile as the documented setup) and `iam/runbook.md` (a note
  that this project's real role was externally provisioned, not created via its own steps 1–4). The
  literal ARN itself was deliberately kept out of every committed file, matching this project's
  existing convention for account IDs — it lives only in the local, gitignored `~/.aws/config` and
  `.env`.
- 2026-08-06 — Resolved the ECR/OIDC blocker (section 8, 7.6). A dedicated role
  (`intern-shruti-ci-role`) was created specifically for the pipeline, separate from the agent's own
  read-only role. Surfaced along the way: the sandbox identity actually can create IAM roles via the
  CLI, just gated behind a required `--permissions-boundary` argument the console's create-role flow
  doesn't support — so "blocked on `iam:CreateRole`" was a narrower constraint than originally
  understood, not an absolute one. The trust policy and permissions policy attached are exactly
  `ci/oidc-trust-policy.json` and `ci/ecr-push-policy.json` (the latter corrected first — it still
  had placeholder ECR repo names that never matched the two repos actually created, a real drift bug
  found while double-checking before use). Wired the role's ARN into the workflow as a GitHub repo
  Variable (`AWS_ECR_PUSH_ROLE_ARN`, set via `gh variable set` — an identifier, not a secret, same
  reasoning as keeping the read-only role's ARN out of committed files) rather than hardcoding it,
  added the `id-token: write` permission the workflow needs to request an OIDC token (previously
  missing, since the temporary static-credential path never needed it), and replaced the temporary
  SSO-session-credentials step with real `role-to-assume`. The three temporary secrets are left in
  place for now, pending a decision on cleanup. Updated `ci/runbook.md` to record how the role was
  actually created (permissions-boundary path, real names) versus its original unconstrained
  self-service description. The push that landed this wiring auto-triggered the workflow (it touches
  `CloudCopilot/**`); watched it run for real — OIDC authentication succeeded, both images pushed,
  confirmed present in ECR via `aws ecr describe-images` afterward. Also manually triggered a second,
  fresh run via `gh workflow run` to watch live.
- 2026-08-06 — Manual functional testing (via Docker Compose) surfaced two real bugs, neither caught
  by the automated test suite:
  - **The agent image crashed on startup** with `FileNotFoundError:
    /app/iam/granted-actions.json`. `agent/permissions.py` reads that file at import time, but
    `agent/Dockerfile` only ever copied `agent/` into the image — `iam/` was never included. Every
    test that exercises this path runs from the repo checkout, where `iam/` sits right next to
    `agent/`, so this gap was invisible to pytest and only surfaced by actually running the built
    container — the same category of miss as the Docker lessons from the original Docker-phase
    sessions (see learning-notes.md). Fixed by copying just `iam/granted-actions.json` into the
    image (nothing else under `iam/`/`ci/` is read by the running agent, and neither contains
    secrets).
  - **A real, reproducible routing bug, not a fluke:** "What VPCs exist in my account?" was
    confidently answered with an EC2-instance list instead of declining or using dispatch — the
    model matched `ec2_uptime` (registry) instead of `run_curated_aws_call` with
    `ec2:describe_vpcs`, 4/4 times in isolated testing. Diagnosed directly (calling the real OpenAI
    API with the actual tool schemas, not guessed): `ec2_uptime`'s description never mentioned what
    it *doesn't* cover, and its name's `ec2_` prefix likely primed the model to treat it as the
    general "anything EC2-account-related" tool rather than specifically "running instances and
    uptime." Fixed by adding one explicit exclusion sentence to `ec2_uptime`'s description; verified
    the fix directly the same way the bug was found — 5/5 correct after the change, both via the
    isolated OpenAI-only test and the real container. Confirmed dispatch itself was already working
    correctly for RDS (`describe_db_instances`, with and without ranking) — that part of the user's
    original report turned out to be a stale Docker image (built 2026-08-04, before dispatch existed
    at all — 2026-08-05), not a routing problem; rebuilding alone fixed it.

  **Hardened against both classes of bug recurring, not just fixed once:**
  - **`scripts/ask.sh`** rebuilds both images every single invocation before running — the "forgot
    to rebuild" step is removed entirely rather than left as something to remember. Documented in
    `README.md` as the recommended way to run a question via Docker; the raw `docker compose`
    commands remain documented too, for finer control.
  - **`.github/workflows/CloudCopilot-ci.yml` now smoke-tests both images** — actually running each
    one (`python -c "import agent.router"` / `import audit_log.app"`) right after building, before
    Trivy or the push. Verified this isn't just a plausible-looking step: rebuilt the agent image
    from the pre-fix Dockerfile and confirmed the exact same smoke-test command fails with the exact
    `FileNotFoundError` this bug produced, then confirmed the fixed image passes — the same "prove
    the fix's own effectiveness, don't just trust that it looks right" discipline as the Semgrep
    suppression check in section 7.6. A build-scan-push pipeline that builds and scans an image but
    never runs it can ship something that crashes on the very first import; this closes that gap for
    both images, permanently, not just for this one missing file.
- 2026-08-06 — Added a read-only dashboard on the audit-log service (`GET /dashboard`, backed by a
  new `GET /stats` aggregation endpoint) — see 7.5. Deliberately scoped as an addition on top of
  already-built, already-tested infrastructure (the same `EventStore`, the same filter semantics as
  `/events`) rather than new surface area — a genuine extra, not something the mission asked for, so
  it stayed additive rather than displacing the still-outstanding 10-question demo. `storage.py`'s
  raw-SQL execution was consolidated into one `_raw_query` helper while adding this — `query()` and
  `stats()` both used to build their own f-string SQL inline, which would have meant the Semgrep
  false-positive suppression comment (7.6) needing to be repeated five more times for `stats()`'s own
  queries; one shared, well-justified execution point was the actual fix, re-verified by re-running
  Semgrep and confirming zero findings, not by assuming the refactor made the warning go away. 112
  tests, 100% coverage on `storage.py`.
- 2026-08-06 — Closed the loop on the dashboard's own dependency-visibility gap the moment it was
  noticed, rather than leaving it silent: `agent/cli.py`'s `_default_audit_log()` now prints a
  one-line stderr note whenever it falls back to `FileAuditLog` (no `AUDIT_LOG_URL` set), explaining
  that this run won't show up on the dashboard and how to fix it — verified it fires for a bare
  `python -m agent` run and stays silent when `AUDIT_LOG_URL` is set (a real Docker Compose run),
  not just asserted in a test. `dashboard.html`'s subtitle now states this scope directly. Also added
  two columns to the recent-events table: `Preview` (the already-collected, already-truncated
  response preview, previously only reachable via a tooltip) and `Calls` (how many AWS calls the
  same question triggered, computed client-side by grouping the fetched page by `question_id` —
  the same "one audit row per real call, not per question" distinction 7.5 already documents,
  now visible rather than only inferable from repeated question text). 113 tests.
- 2026-08-06 — Two dashboard fixes from actually looking at it critically after use. The `Preview`
  column added above turned out low-value in practice: a truncated raw Python-dict repr reads fine
  in a debug log, not at a glance in a UI, and it added nothing for a successful call the
  Service:operation column didn't already say — removed rather than kept for its own sake. The
  `Time` column was displaying the stored UTC timestamp with a naive string-slice, never converted
  — correct data, wrong read for anyone not in UTC (this sandbox runs in `ap-south-1`/IST, a 5.5-hour
  gap). Fixed by parsing it as a real `Date` and rendering with the *viewer's* local timezone
  (`toLocaleString`), not a hardcoded offset — verified against a known event's stored UTC value
  translating to the expected IST wall-clock time, not just that a `Date` object exists.
- 2026-08-06 — Added a short reason next to every declined/failed row on the dashboard
  (`Declined (blocked: missing IAM action(s) ce:GetCostAndUsage)`). Doing this honestly meant
  discovering most failure paths didn't actually capture a reason at all yet, not just that the
  dashboard wasn't displaying one: only the permission gate's own decline (7.6/section 8) ever
  populated `preview` on failure — dispatch's deny-list/not-allowed block and every genuine AWS
  exception (in both `registry.py` and `dispatch.py`) passed `None`. Added `registry.py`'s
  `_failure_preview(exc)` — AWS's own error *code* (`AccessDenied`, `Throttling`, …) for a real
  `ClientError`, the exception's class name otherwise — deliberately never the full exception
  message verbatim, extending this project's "no full raw content in the audit log" principle
  (7.5) to the failure path, not just successful responses (a `ParamValidationError` message can
  echo back a caller-supplied value). Dispatch's own deny-list block now reuses `DENYLIST`'s
  existing reason strings rather than discarding them at audit time. Existing tests updated to
  match (one assumed a denylisted-vs-merely-unlisted operation that turned out to already be
  denylisted — `dynamodb.scan`, not `dynamodb.list_tables`), plus new direct tests for
  `_failure_preview` itself. 115 tests. Verified end-to-end through the real containerized path,
  not just unit tests: rebuilt both images, triggered a real permission-gate decline, confirmed
  the exact bracketed reason rendered on the live dashboard.
- 2026-08-06 — Unified the two remaining decline paths (7.7) that didn't yet match the permission
  gate's "insufficient access + suggestion" pattern, prompted by a direct question about whether
  #4 (a genuine AWS-side `AccessDenied` our proactive gate didn't anticipate) really deserved the
  generic "please try again shortly" message — it didn't; retrying a permissions problem never
  helps, and it's actively misleading to imply otherwise. `agent/router.py` now catches this
  reactively in both the registry and dispatch branches, checking `_failure_preview(exc)` against a
  new `agent/registry.py` constant, `ACCESS_DENIED_ERROR_CODES` (`AccessDenied`,
  `AccessDeniedException`, `UnauthorizedOperation` — not exhaustive by design), and routes it
  through the exact same `_permission_gap_answer` the proactive gate already uses — same wording,
  same suggestion mechanic, whether the gap was caught before or after the AWS call. For the
  dispatch tier's two *other* decline reasons (deny-listed by name, or simply never reviewed onto
  `ALLOWLIST`) — conceptually different from an access problem, since more IAM permissions
  wouldn't fix either — `DispatchNotAllowed` now carries the specific reason (from `DENYLIST`
  itself) as a real attribute rather than a fixed message string, so `agent/router.py` can word a
  deliberate security restriction ("it's restricted for security reasons (returns actual file
  contents)") distinctly from "never reviewed yet" — no suggestion appended to either, since this
  is a fixed policy decision, not a deployment gap. 119 tests. Verifying the two new reactive
  branches needed monkeypatching `dispatch.run_curated_call`/a fake `RegistryEntry` respectively,
  since coaxing the real model into deliberately attempting something it correctly avoids isn't a
  meaningful test — confirmed the model does exactly that (declines via the suggestion tier before
  ever reaching dispatch) when tried live, which is the *correct* behavior, not a gap to work around.
- 2026-08-06 — Added terminal styling for the interactive/single-question CLI (`agent/style.py`) —
  a bold headline, cyan-marked bullets in place of plain `- `, a styled prompt and welcome banner.
  Deliberately a separate, later presentation layer applied only in `agent/cli.py` at the point of
  printing, never inside `agent/router.py`'s own `_format_*` functions — those stay plain, testable
  text (dozens of `tests/test_router.py` assertions depend on exact substrings), and this module
  works purely structurally on top of them (bold the first line, restyle any `- `-prefixed line)
  without needing to know what any formatter's output means. Colour is skipped automatically —
  never a flag a caller has to remember — whenever the destination isn't a real terminal (a pipe,
  a `NO_COLOR`-respecting convention, or every existing test's `StringIO`/`capsys` stream, which
  report `isatty()` as `False`) — confirmed this holds by checking real output through three paths:
  a bare pipe (plain, no escape codes), a forced pseudo-TTY locally via `script` (styled, verified
  by decoding the raw bytes rather than trusting `cat -v`'s caret-notation rendering of the
  multi-byte bullet character), and the real Docker Compose path (`tty: true` in
  `docker-compose.yml` already allocates one, so `docker compose run`/`scripts/ask.sh` render
  styled automatically with no config needed). No new dependency — plain ANSI escape codes. 130
  tests, 100% coverage on the new module.
- 2026-08-06 — Found, while preparing for a full ~100-150 question test pass, that
  `iam/granted-actions.json` was itself wrong about four actions: `s3:ListAllMyBuckets`,
  `iam:ListRoles`, `logs:FilterLogEvents`, and `logs:DescribeLogGroups` are present in the agent
  role's identity-based policy (which is where that file's contents originally came from) but are
  actually blocked by a separate **permissions boundary** on the same role
  (`arn:aws:iam::767398067510:policy/intern-permissions-boundary`) — the exact same class of gotcha
  already documented for the CI role (7.6, `ci/runbook.md`), just discovered here by testing rather
  than being told about it upfront. Confirmed directly, not assumed: each of the four returns
  `AccessDenied`/`AccessDeniedException` with an explicit "no permissions boundary allows..."
  message, distinct from a legitimate not-found response — verified that distinction by also
  testing `s3:ListBucket`, `s3:GetBucketPolicyStatus`, and `eks:DescribeCluster` against nonexistent
  resources and getting real `NoSuchBucket`/`ResourceNotFoundException` errors instead, proving
  those three are genuinely fine. The first two don't change any currently-observed behavior
  (the registry capabilities that need them were already blocked by other missing actions), but the
  last two are new and real: **both of dispatch's CloudWatch Logs capabilities — the count-only
  `logs:FilterLogEvents` and plain `logs:DescribeLogGroups`, built and moto-tested in the previous
  session — are non-functional against the actual account.** The system still degrades gracefully
  (`agent/router.py`'s reactive `AccessDenied` handling, section 8, catches it and gives the correct
  "insufficient access + suggestion" answer — verified this directly too, both that the model
  attempts the call and that the answer comes back correct) — but `granted-actions.json` has been
  corrected to move all four into a new `_blocked_by_permissions_boundary` section, so the
  *proactive* gate catches this before ever spending a real AWS call, rather than relying on the
  reactive fallback alone. Updated `tests/test_permissions.py`'s regression test to match the
  corrected, larger gap (20 actions now, up from 16). No behavior change to any code, only to the
  data describing what's actually usable.
- 2026-08-06 — A ~140-question live test pass (running `answer_question` directly against the real
  account, not through Docker, to avoid per-question container overhead) surfaced two more real
  routing bugs before an expired local SSO session cut the run short at question 47 — see the next
  entry for that. Both bugs are the same underlying failure mode already found once this session
  (`ec2_uptime` over-matching "what VPCs exist") recurring in new shapes, proving that fix was too
  narrowly scoped:
  - `"List the objects in the <bucket> S3 bucket"` matched the registry's `s3_public_access`
    (checks whether a bucket blocks public access) instead of the general dispatch tool listing a
    bucket's actual contents — same tool, wrong resource-type generalization, this time on `s3_`
    rather than `ec2_`.
  - `"How many EBS snapshots do I have?"` and especially `"What Elastic IP addresses am I using?"`
    both matched `ec2_uptime` again, despite last session's fix explicitly calling out "VPCs,
    subnets" — the fix excluded the *specific case found*, not the *general pattern*, so a new
    EC2-adjacent resource type just reopened the same bug from a different angle. "Elastic IP"
    turned out to be the stubborn one: broadening `ec2_uptime`'s own exclusion list to explicitly
    name it, and even adding a same-meaning hint to the dispatch tool's own allow-list description,
    did *not* fix it on their own (3/3 wrong, repeatedly) — likely because "Elastic IP" doesn't
    obviously map to the boto3 operation name `describe_addresses` the way "VPCs" maps to
    `describe_vpcs`, so the model fell back to the tool it already had high confidence in. What
    actually worked: one explicit worked counter-example added directly to `SYSTEM_PROMPT`'s own
    routing instructions (naming this exact question and the exact fix), the same technique that
    already fixed the sort_by/limit reliability issue last session — verified 3/3 correct
    afterward, confirmed no regression across 9 other cases (registry and dispatch alike, 3 runs
    each), and confirmed the "flaky"-looking cases in that check were only cosmetic argument
    formatting (`params: {}` included or omitted), not real routing differences.
- 2026-08-06 — Rewrote `SYSTEM_PROMPT` and added multi-call support (7.2, 7.7), on request, after
  the ~140-question test pass surfaced too many compound questions getting refused outright rather
  than partially answered. Two real, tested changes, not one:
  - **Multi-call**: `answer_question` now runs every tool call in the model's one response (capped
    at `MAX_CALLS_PER_QUESTION = 5`), not just the first, combining each call's own formatted
    outcome — success, a permission gap, a security restriction — into one answer. A question with
    an answerable part and a blocked part now gets a real answer for the first and an honest
    explanation for the second, instead of the old "refuse the whole thing" behavior the previous
    prompt's "don't force a partial match" rule produced. The single-call case (still the large
    majority of questions) is provably unchanged: `"\n\n".join([one_answer])` is just `one_answer`,
    and all 130 pre-existing tests passed unmodified. This is *independent* calls decided in one
    turn, not dependent fan-out — see 7.7 for that distinction and why it's deferred.
  - **A shorter, plainer prompt** — but not as short as first attempted. An initial aggressive
    simplification (roughly half the original length) was tested against every previously-fixed
    routing case and found two real regressions, not just permissible drift: "which RDS instance is
    newest?" dropped from reliably shaped to only 3/6 runs including `sort_by`/`limit` at all, and
    "list the objects in bucket X" dropped from working to 0/6. Both regressions trace to the same
    mistake — replacing an imperative instruction *with a concrete worked example* with a shorter,
    softer general statement. The fix wasn't reverting to the original's length or structure, it was
    keeping the specific worked examples that were already empirically load-bearing (the RDS
    sort_by/limit example from Session 2, an Elastic-IP-vs-`ec2_uptime` example, and a new one
    distinguishing `s3_public_access` from listing a bucket's actual contents) while still cutting
    what was genuinely obsolete verbosity (the old step-by-step "don't force a partial match, treat
    as unmatched" framing, now replaced by multi-call actually answering the parts it can). Verified
    the final version 6/6 across seven previously-fragile cases before shipping it, then again live
    through the rebuilt container for both a multi-call success case and a mixed
    success-plus-blocked case. 134 tests.
- 2026-08-06 — After the user pasted a full spec of expected behavior across every service, sorted
  it into what's genuinely buildable now (no new IAM actions needed, just better use of what
  `granted-actions.json` already allows) versus what's blocked by the real permissions boundary
  (section 8) and would need a new ask to whoever administers the sandbox. Built the buildable
  list:
  - **EC2** — `ec2_uptime` renamed `ec2_instances` and given a real `state` parameter
    (`"running"` by default, `"stopped"` or `"all"` to broaden it) plus much richer per-instance
    detail: type, AMI, public/private IP, VPC, availability zone, security groups, attached
    volumes, and name tag, not just uptime. `min_uptime_hours` still applies, but only to running
    instances now (a stopped instance has no uptime to filter on).
  - **RDS** — `describe_db_instances`/`describe_db_clusters` promoted out of dispatch's raw-dump
    ALLOWLIST into hand-formatted registry entries (`rds_instances`/`rds_clusters`), matching how
    EC2 and security groups already work, once real usage showed these were common enough to
    deserve real formatting instead of a generic dict dump.
  - **EKS** — `list_clusters`/`describe_cluster` added to dispatch's ALLOWLIST (cluster name,
    status, version, endpoint) — genuinely new coverage, not a promotion, since nothing covered EKS
    before at all.
  - `iam/policy.json` updated to match (RDS actions moved into the fixed-registry statement, EKS
    actions added to the dispatch statement) — the drift-check test caught the EKS gap immediately
    when it was missed on the first pass, exactly as designed.
  - `SYSTEM_PROMPT`'s existing worked examples were re-checked against every field-name change
    before shipping, not after: the Elastic-IP example now says "the ec2_instances tool only covers
    EC2 instances themselves" (still true, but referencing the renamed tool), and the RDS
    sort_by/limit example was changed from the raw AWS field name `InstanceCreateTime` to the new
    registry function's own field name `create_time` — this one was caught proactively, before it
    could silently break the very reliability fix Session 2 had already verified.
  - 148 tests, 99.5% coverage (added dedicated tests for the `state="stopped"`/`"all"` filtering,
    `min_uptime_hours` being ignored for non-running states, the richer per-instance fields, and
    both new RDS formatters).
  - **Live verification against the real account surfaced one more real routing bug**, the same
    failure mode as the Elastic-IP and S3-listing cases above: "what EKS clusters exist in my
    account?" was only routed correctly 1/5 times — the other 4 runs picked
    `iam_roles_users_and_groups` or `ec2_instances` instead, neither of which have anything to do
    with EKS. Being on dispatch's ALLOWLIST and named in its tool description wasn't enough on its
    own; fixed the same way as before, with one explicit worked example added to `SYSTEM_PROMPT`
    naming the exact question, the correct `(service="eks", operation="list_clusters")` call, and
    the two specific wrong tools not to use. Verified 6/6 correct afterward, with no regression on
    the Elastic-IP, RDS-ranking, or S3-listing cases re-checked alongside it (confirmed via the
    real OpenAI API's chosen tool calls directly, not just the final answer text).
- 2026-08-07 — Investigated why `s3:ListAllMyBuckets`, `iam:ListRoles`, `logs:FilterLogEvents`,
  and `logs:DescribeLogGroups` still don't work despite being present in the role's identity
  policy. The user shared the real permissions-boundary policy text
  (`arn:aws:iam::767398067510:policy/intern-permissions-boundary`, `Sid:
  BoundaryAllowsOnlyMissionServices`) — confirmed it's a narrow allow-list scoped to the agent's
  own runtime needs (log emission, S3 object read/write, ECR push for CI) that never anticipated
  these later-added *auditing* capabilities (bucket enumeration, IAM listing, log querying). No
  code fix exists for this — a permissions boundary is enforced by AWS itself, independent of what
  the identity policy or this project's own code say. Drafted the exact 4-action addition needed
  for whoever administers that boundary to apply; not applied by this project, since it's an
  account-level control outside this project's own access.
- 2026-08-07 — Same investigation surfaced a *different* gap for ECR: `ecr:DescribeRepositories`
  turned out to be already allowed by the boundary (confirmed directly in its text), but the
  role's own identity policy (`CloudOpsCopilotReadOnly`) never granted it at all — only push-side
  ECR actions for the CI/CD role's benefit. Confirmed by the AccessDenied wording itself: "no
  identity-based policy allows the ecr:DescribeRepositories action", the opposite phrasing from a
  boundary denial. Built the new capability anyway, since the fix needed here (one action added to
  the identity policy, not the boundary) is real but smaller, and building it now means it starts
  working the moment that one grant lands, without a second round of implementation:
  - Added `ecr:DescribeRepositories` to `agent/dispatch.py`'s ALLOWLIST — no hand-written formatter,
    same treatment as EKS, using the existing generic dispatch formatting.
  - `iam/policy.json` updated to match; `iam/granted-actions.json` gained a new
    `_blocked_by_missing_identity_grant` section, deliberately separate from
    `_blocked_by_permissions_boundary` — same practical effect (the proactive gate declines it
    before any real AWS call) but a different real cause, documented honestly rather than lumped
    in with the boundary findings.
  - Verified routing directly against the real OpenAI API across several phrasings ("what ECR
    repositories exist", "how many container image repositories", "list my docker image repos") —
    5/5 and more all routed correctly with no worked example needed in `SYSTEM_PROMPT`, unlike
    EKS's dispatch entry; confirmed no regression on the EKS and Elastic-IP cases fixed earlier.
  - Verified the permission gate declines cleanly against the real account today (identity policy
    doesn't yet grant this), with the correct specific message rather than a raw AccessDenied.
  - 149 tests, one new moto-backed test for the dispatch entry, one existing regression test
    (`test_permissions.py`'s locked-in gap set) updated to include the new, honestly-documented
    gap.
- 2026-08-07 — On request, audited every action in `iam/granted-actions.json`'s `actions` list
  (genuinely, empirically granted right now) against what registry/dispatch code actually uses —
  computed the set difference directly rather than eyeballing it. Found 6 granted actions with zero
  code wired to them at all: `s3:GetBucketPolicyStatus` and five EC2 networking describes
  (`DescribeSubnets`, `DescribeRouteTables`, `DescribeInternetGateways`, `DescribeNatGateways`,
  `DescribeNetworkAcls`). Added all 6 to `agent/dispatch.py`'s ALLOWLIST (generic dispatch
  formatting, no hand-written formatter, same treatment as EKS/ECR) and synced `iam/policy.json`.
  No `granted-actions.json` change needed — unlike the EKS/ECR round, these were already fully
  granted, not partially blocked.
  - **Live verification surfaced two more real routing bugs**, the same recurring failure mode as
    the Elastic-IP case from Session 3 and the EKS case from Session 4 — a one-off patch per
    resource type kept reopening on the next new EC2-adjacent operation added: "what route tables
    exist?" misrouted to `ec2_instances` 3/3 times, and "what NAT gateways am I using?" misrouted to
    `ec2_instances` 1/2 times. Rather than patch these two narrowly (the pattern that kept
    recurring), rewrote `SYSTEM_PROMPT`'s EC2 disambiguation into one general rule naming the whole
    family at once — Elastic IPs, volumes, snapshots, VPCs, subnets, route tables, internet
    gateways, NAT gateways, and network ACLs are all explicitly listed as separate from instances —
    instead of adding a 6th one-off exception. Verified 3/3 on route tables and NAT gateways
    afterward, plus subnets/internet-gateways/network-ACLs/Elastic-IP/EKS/RDS-ranking/EC2-instances
    all still correct (checked via the real OpenAI API's chosen tool calls directly).
  - "Is the bucket policy on bucket X public?" also misrouted, to the registry's `s3_public_access`
    3/5 times, even after being folded into the same dense EC2-disambiguation paragraph as the fix
    above — a general instruction crammed alongside four unrelated examples wasn't enough on its
    own. Fixed by giving it its own dedicated paragraph right where `s3_public_access`'s actual
    scope is first introduced (next to the existing list_objects_v2-vs-bucket-contents
    distinction), naming the exact confusion ("both mention 'public'") directly. Verified 8/8
    afterward, with "which S3 buckets are publicly accessible?" (a real `s3_public_access` question,
    not a policy question) still routing correctly alongside it — confirming the fix narrowed the
    right case without over-correcting the adjacent one.
  - Verified `s3:GetBucketPolicyStatus` and all 5 EC2 networking describes end-to-end against the
    real account (moto tests plus live AWS calls), and route tables through the rebuilt Docker
    container. 151 tests, 99.5% coverage, ruff and Semgrep clean.
