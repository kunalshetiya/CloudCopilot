# Running CloudCopilot locally

Practical "how do I run this on my machine" steps — separate from `context.md` (decisions and why)
and `iam/runbook.md` (creating the intended read-only role, currently blocked — see `context.md`
section 8). This is about running the agent today, against the sandbox account, using the
temporary auth workaround that section documents.

## 1. Python environment

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

## 2. `.env` file

Create a `.env` file in `CloudCopilot/` (already gitignored — never commit real values):

```
OPENAI_API_KEY=sk-...
AWS_PROFILE=cloudcopilot-agent
```

`AWS_PROFILE` names a *role-assuming* profile configured in step 3 — chained on top of your SSO
profile, not raw access keys. This file's contents are static once set up; refreshing an expired
SSO session (step 4) never requires editing it.

## 3. AWS SSO + role-assumption setup (one-time)

Two profiles, chained together — an SSO profile for your own identity, and a second profile that
assumes the agent's read-only role on top of it:

```
aws configure sso
```

Interactive wizard: your organization's SSO start URL, the SSO region, then it lists which
account + permission set you have access to, and finally asks you to name the profile (any name —
this doc calls it `<your-sso-profile>` below) and pick a default region. Writes a
`[profile <your-sso-profile>]` block into `~/.aws/config` — entirely separate from the Python
virtual environment; it doesn't matter whether `.venv` is active when you run this.

Then add a second profile by hand to `~/.aws/config`, right below the SSO one:

```ini
[profile cloudcopilot-agent]
role_arn = <the read-only role's ARN, provided separately — never commit this>
source_profile = <your-sso-profile>
region = ap-south-1
output = json
```

`role_arn` is the agent's actual read-only role (docs/context.md section 8) — provisioned
externally, not something this project creates. `source_profile` is whichever identity above has
permission to assume it. With this in place, `AWS_PROFILE=cloudcopilot-agent` (or
`boto3.Session(profile_name="cloudcopilot-agent")`) transparently assumes the role and re-assumes
it automatically whenever the underlying SSO session refreshes — no manual credential export, ever.
Verify it worked with `aws sts get-caller-identity --profile cloudcopilot-agent --query Arn` — the
returned ARN should be an `assumed-role/<the role name>/...` ARN, not your own SSO identity.

## 4. Log in (whenever the session expires)

```
aws sso login --profile <your-sso-profile>
```

Opens a browser to complete SSO auth, then caches temporary credentials locally. This is the only
step that needs repeating over time — SSO sessions expire (typically hours, not days). Nothing
else in this setup needs to change when that happens: `.env`'s `AWS_PROFILE` value stays
`cloudcopilot-agent`, and the CLI/boto3 automatically re-assumes the role using the refreshed SSO
session underneath it.

**Why this can't be avoided entirely:** the underlying SSO identity is IAM Identity Center-
federated, which only ever issues temporary session credentials — there's no way to get a
permanent, non-expiring access key from an identity of this kind.

**A note on what this role actually grants:** the provided role's attached policy doesn't match
`iam/policy.json`'s aspirational scope exactly — some things are missing (notably all of Cost
Explorer), some things it grants weren't part of the original design. See `docs/context.md` section
8 and `iam/granted-actions.json` for the real, current picture; `agent/permissions.py` declines
gracefully (with a suggestion) for anything the role doesn't actually cover, rather than showing a
raw `AccessDenied`.

## 5. Load `.env` and run

Each new terminal session:

```
set -a
source .env
set +a
```

Then either:

```
python -m agent "which ec2 instances have been running more than 24 hours?"
```

or, for an interactive session:

```
python -m agent
```

## 6. Running the audit-log service (optional, for real end-to-end testing)

In a separate terminal (same venv):

```
uvicorn audit_log.app:app --port 8001
```

Then point the agent at it instead of the local file — e.g. in a small script:
`answer_question(question, audit_log=HttpAuditLog(base_url="http://localhost:8001"))`. Browse what
got recorded with `curl http://localhost:8001/events`. (The CLI itself still defaults to
`FileAuditLog` — wiring the CLI to use `HttpAuditLog` by default is part of the upcoming Docker
Compose phase, not done yet.)
