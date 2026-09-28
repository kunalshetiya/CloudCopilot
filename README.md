# CloudCopilot

CloudCopilot is a conversational, read-only AWS operations agent. Ask it a plain-English
question about a live AWS account — like *"which EC2 instances have been running more than 24
hours?"* — and it figures out the right AWS API call, makes it, and answers. A companion
audit-log service records every question and every AWS call it makes, so nothing happens out of
view.

Two things make this safe to point at a real account: it can never change anything (every
permission it has is enforced by AWS itself, not just by the code behaving), and every action it
takes is logged and reviewable.

## Quick start

**1. One-time setup** — Python environment and AWS/OpenAI credentials. See
[`docs/local-setup.md`](docs/local-setup.md) for the full walkthrough.

**2. Run both services:**

```bash
# Start the audit-log service (stays running)
docker compose up -d audit-log

# Ask a question — always rebuilds first, so you're never running stale code
scripts/ask.sh "which ec2 instances have been running more than 24 hours?"

# Or start an interactive session (no question = keep asking)
scripts/ask.sh
```

**3. Watch the audit trail** — open **http://localhost:8001/dashboard** in a browser to see every
question and every AWS call, live, with charts and a filterable event table.

## What it can answer

Ask about EC2 instances (state, network, storage, uptime), security groups open to the internet,
S3 bucket contents, VPCs, RDS instances and clusters, EKS clusters, and more — see
[`docs/context.md`](docs/context.md) section 7.3 for
the full, current list of what's actually usable against this project's sandbox account (a few
things the code supports are currently blocked by an account-level permission restriction; the
agent explains this itself, with a suggestion, rather than failing silently).

## How it works, briefly

1. **A small set of hand-written, hand-tested tools** answer the most common questions directly
   (e.g. "list running EC2 instances").
2. **A curated fallback** lets the AI pick from a reviewed allow-list of other read-only AWS
   calls for anything the specific tools don't cover — it only ever picks a *(service, operation,
   parameters)* triple; our own code is what actually calls AWS, so the AI never runs code of its
   own.
3. **A general suggestion, with no AWS access at all**, for anything else that's still a genuine
   AWS question — never a call, never fabricated account data.

A question can need more than one of these calls at once (e.g. "what EC2 instances and VPCs do I
have?") — the agent runs every call it genuinely needs and combines the real answers, rather than
refusing the whole question if only part of it is covered.

IAM is the backstop underneath all of this: the role the agent runs as only ever grants
`Describe`/`List`/`Get`-style read actions, hand-reviewed one at a time — never anything that
could modify the account, and never anything that reads secrets, file contents, or raw log text.

The full reasoning behind every one of these decisions — and the tradeoffs considered — lives in
[`docs/context.md`](docs/context.md).

## Status

Every mission deliverable is built and verified: the capability registry and curated fallback,
a hand-authored read-only IAM policy with an automated drift-check, the audit-log service, a
two-service Docker Compose setup on a custom network, and a full CI/CD pipeline (lint → tests
with a 75% coverage gate → Semgrep → Trivy → push to ECR) — all confirmed working against real
infrastructure, not just in tests. **151 tests, 99.5% coverage.**

Both AWS roles this project depends on (the agent's own read-only role, and a separate role the
CI/CD pipeline uses to push images) are live and have been exercised for real — real AWS calls
through the read-only role, and a real GitHub Actions run that authenticated via OIDC and pushed
real images to ECR.

**One open item:** the account's real permissions turned out narrower than originally expected in
a few places (documented honestly in `iam/granted-actions.json` and `docs/context.md` section 8)
— the agent handles this gracefully today, but it does mean not every built capability is
currently answerable against this specific sandbox account.

See [`docs/context.md`](docs/context.md) section 9 for the full, dated history of every decision.

## Project layout

```
CloudCopilot/
├── agent/
│   ├── registry.py      # hand-written capabilities for common questions
│   ├── dispatch.py      # curated fallback: allow-list + deny-list of other AWS calls
│   ├── permissions.py   # checks a capability against what's actually granted
│   ├── router.py        # routes a question to a capability, or a suggestion
│   ├── audit.py         # sends each AWS call to the audit-log service
│   ├── cli.py            # single-question and interactive modes
│   ├── style.py           # terminal styling for interactive use
│   └── Dockerfile
├── audit_log/
│   ├── app.py            # FastAPI service: records and serves the audit trail
│   ├── storage.py         # SQLite-backed event store
│   ├── dashboard.html      # the live audit dashboard
│   └── Dockerfile
├── iam/                      # the hand-authored, read-only IAM policy (never auto-applied)
├── ci/                        # OIDC trust policy, ECR push policy, and setup runbook
├── tests/                      # unit tests (pytest + moto)
├── scripts/ask.sh                # run a question via Docker, always rebuilding first
├── docker-compose.yml              # both services, on a shared custom network
└── docs/
    ├── context.md                   # every architecture decision, and why
    ├── learning-notes.md             # the reasoning and concepts behind those decisions
    ├── local-setup.md                 # how to run this on your own machine
    └── mission-spec.md                 # the original assignment, kept verbatim
```

The CI/CD pipeline definition itself lives at the repo root (GitHub Actions requires this):
`.github/workflows/CloudCopilot-ci.yml`.
