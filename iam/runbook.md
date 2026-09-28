# Runbook: creating and using the agent's read-only IAM role

This is the manual, deliberate step referenced in `docs/context.md` section 7.4 — a person runs
these commands by hand. Nothing here is automated, and CI/CD never runs any of this.

`iam/policy.json` is the hand-authored permissions policy (what the role is allowed to do).
`iam/trust-policy.json` is a template for the trust policy (who is allowed to *become* the role) —
fill in the placeholders before using it; don't commit your filled-in version with your real
account ID/username in it.

**As of 2026-08-06, this project's actual role wasn't created via steps 1–4 below** — a read-only
role was provided directly by whoever administers the sandbox account, and its attached policy
turns out not to match `iam/policy.json` exactly (see `docs/context.md` section 8 and
`iam/granted-actions.json` for the real, current gap). If you're in that same situation — someone
handed you a role ARN rather than asking you to create one — skip straight to step 5 to verify you
can assume it, then see `docs/local-setup.md` section 3 for wiring it into a local AWS profile
(`role_arn` + `source_profile` chaining, so it re-assumes automatically). Steps 1–4 below remain
useful as the *intended* self-service process, for whenever creating the role directly becomes
possible.

## 1. Confirm your own identity

```
aws sts get-caller-identity
```

Note the `Account` (your sandbox account ID) and `Arn` (your IAM user's ARN) from the output —
both go into `iam/trust-policy.json` in the next step.

## 2. Fill in the trust policy template

Copy `iam/trust-policy.json` to a local, uncommitted file (e.g. `iam/trust-policy.local.json`) and
replace `<ACCOUNT_ID>` and `<YOUR_IAM_USERNAME>` with the values from step 1.

## 3. Create the role

```
aws iam create-role \
  --role-name CloudCopilotReadOnly \
  --assume-role-policy-document file://iam/trust-policy.local.json \
  --description "Read-only role for the CloudCopilot agent"
```

## 4. Attach the permissions policy

```
aws iam put-role-policy \
  --role-name CloudCopilotReadOnly \
  --policy-name CloudCopilotFixedRegistryReadOnly \
  --policy-document file://iam/policy.json
```

## 5. Try to assume it

```
aws sts assume-role \
  --role-arn arn:aws:iam::<ACCOUNT_ID>:role/CloudCopilotReadOnly \
  --role-session-name cloudcopilot-test
```

This is also the real test of whether your own identity has `sts:AssumeRole` permission at all —
more definitive than predicting it with `simulate-principal-policy`, since this is the actual call.

- **If it succeeds:** you get back `AccessKeyId`, `SecretAccessKey`, and `SessionToken` with an
  expiration (default: 1 hour). Continue to step 6.
- **If it fails with `AccessDenied`:** your identity genuinely can't assume roles right now, and
  this path is blocked — see `docs/context.md` section 8 for the fallback (a dedicated, purpose-
  built IAM user with the same `iam/policy.json` attached directly, no assume-role involved).

## 6. Use the temporary credentials

Export the three values from step 5's output, plus your OpenAI key (separate from all of this —
needed for the agent's LLM routing calls, not for AWS):

```
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...
export OPENAI_API_KEY=...
```

Then run the agent directly:

```
python -m agent "which ec2 instances have been running more than 24 hours?"
```

For a containerized run, the same three `AWS_*` variables (plus `OPENAI_API_KEY`) get passed into
the container the same way — e.g. via Docker Compose's `environment:` — once that's built.

Session credentials expire (step 5's `Expiration` field) — re-run step 5 to refresh them.
