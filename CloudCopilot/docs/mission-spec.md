# Mission Spec (verbatim)

Kept word-for-word so we never drift from what was actually assigned. If our implementation ever
disagrees with this file, this file is the ground truth for "what was asked," and `context.md`
is the ground truth for "what we decided to build instead/in addition, and why."

---

## Port·05 — CloudOps Copilot: Conversational AWS Operations Agent

### Mission Description

Cloud accounts accumulate resources fast — EC2 instances, S3 buckets, security groups, IAM
roles — and answering a simple operational question ("which instances have a public IP?",
"what's driving this month's bill?") usually means digging through the console or writing a
one-off script. Build a tool that connects to a real AWS account and answers operational
questions in plain English, using the AWS APIs underneath. The tool decides which AWS action to
run based on the question asked, then calls the AWS API directly. It is read-only in this task,
so it is safe to run against a real account.

### Instructions

TECH STACK: Python + boto3 — AWS SDK. Anthropic Claude / OpenAI — decides which AWS action to
run for a given question. pytest + coverage — unit tests, minimum 75% coverage. ruff — linting.
Semgrep — static code scanning. Trivy — container image vulnerability scanning. Docker + Docker
Compose — the agent plus a companion audit-log service, on a shared network. GitHub + GitHub
Actions — version control and CI/CD. Amazon ECR — container image registry. AWS CLI + IAM —
least-privilege, read-only access. Environment: a dedicated, budget-capped AWS sandbox account
provided per intern.

### Deliverables

- Read-only IAM role scoped to exactly the needed actions.
- boto3 functions covering 5+ AWS operations (EC2, S3, security groups, cost, etc.).
- Unit tests with 75%+ coverage.
- CI/CD pipeline: lint → unit tests (coverage gate) → build image → Semgrep scan → Trivy scan →
  push to ECR.
- Pipeline fails the build on high/critical vulnerabilities or coverage below 75%.
- Two-service Docker Compose setup — the agent plus an audit-log service, on a custom network.
- Demo: 10 questions answered correctly against a live sandbox account, every action visible in
  the audit log.

### Expected Output

A container that runs against a live AWS sandbox account and answers questions such as "which
EC2 instances have been running more than 24 hours?" The AWS role it uses can only read data,
never change it. Every question and every AWS call it makes is recorded by a separate audit-log
service running alongside it.

### Best Practices Expected

IAM role scoped strictly to read-only, reviewed before use. No AWS credentials hardcoded or
committed. Docker images run as non-root on a pinned base image, no secrets baked into layers.
Pipeline fails the build on high/critical vulnerabilities (Trivy) or code issues (Semgrep).
Pipeline fails the build if test coverage is below 75%. Compose services use a defined custom
network and communicate by name, not hardcoded IPs. Every AWS action logged.
