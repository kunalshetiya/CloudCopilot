# Runbook: setting up ECR + GitHub OIDC for CI/CD

Manual, one-time AWS setup for `.github/workflows/CloudCopilot-ci.yml`'s push-to-ECR stage — the
same "hand-run, reviewed, never automated" treatment as `iam/runbook.md`, for the same reason: this
creates real AWS permissions, so a person runs it deliberately, not a script.

**Status: RESOLVED (2026-08-06).** The role exists and is wired into the workflow. It wasn't created
via steps 1–7 exactly as originally written, though — worth recording how it actually happened,
since it changes what "blocked on `iam:CreateRole`" (docs/context.md section 8) turned out to mean:
- The sandbox identity *can* create IAM roles via the CLI, but only with a required
  `--permissions-boundary` argument (a policy, provisioned by whoever administers the sandbox, that
  caps the role's effective permissions regardless of what its own identity-based policy grants).
  The AWS console's own role-creation UI doesn't support specifying this in the same flow, which is
  why it returned `AccessDenied` there — not a hard, no-workaround block, just a narrower path than
  originally assumed.
- The two ECR repositories already existed (created manually earlier, see `docs/context.md` 7.6)
  under real names — `intern-shruti-cloudops-copilot-agent` and
  `intern-shruti-cloudops-copilot-audit-service` — not the `cloudcopilot-agent`/`cloudcopilot-audit-log`
  placeholders steps 3 and 5 originally described; `ci/ecr-push-policy.json` has been corrected to
  match. Step 3 below didn't need to run at all.
- The role is named `intern-shruti-ci-role`, not `CloudCopilotGithubActionsECRPush` — steps 6–7
  below use the placeholder name; substitute the real one.
- `AWS_ACCOUNT_ID`, `AWS_REGION`, and `AWS_ECR_PUSH_ROLE_ARN` are all set as real GitHub repo
  Variables already.

Steps 1–7 remain accurate as the originally-intended, unconstrained self-service process — useful
if a future role doesn't need a permissions boundary, or as a reference for what each piece is for.

## 1. Confirm whether a GitHub OIDC provider already exists in this account

```
aws iam list-open-id-connect-providers
```

Look for one with a URL of `token.actions.githubusercontent.com`. This is an account-wide
resource — if any other project already uses GitHub Actions OIDC in this same AWS account, it may
already exist, and step 2 can be skipped.

## 2. Create the OIDC provider (only if step 1 found none)

```
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

The thumbprint is GitHub's OIDC provider's current root CA thumbprint — AWS also lets you omit
`--thumbprint-list` entirely and it will fetch this automatically in newer CLI versions; confirm
against GitHub's own OIDC documentation if this exact value is ever rejected.

## 3. Create the two ECR repositories

```
aws ecr create-repository --repository-name cloudcopilot-agent
aws ecr create-repository --repository-name cloudcopilot-audit-log
```

## 4. Fill in the trust policy template

Copy `ci/oidc-trust-policy.json` to a local, uncommitted `ci/oidc-trust-policy.local.json`
(gitignore this the same way `iam/trust-policy.local.json` is) and replace `<ACCOUNT_ID>` with your
real account ID (from `aws sts get-caller-identity`).

Note the `StringLike` condition on `token.actions.githubusercontent.com:sub` — it's scoped to
`repo:devops-intern-calfus-org/shruti:ref:refs/heads/main` specifically, meaning only a run
triggered on `main` in this exact repo can assume this role. This is deliberate, defense-in-depth
scoping: the workflow itself also only attempts the push on `main` (see the workflow's own
`if: github.ref == 'refs/heads/main'`), but the trust policy enforces the same restriction
independently at the AWS side, so a compromised or misconfigured workflow couldn't assume this
role from a PR or a fork even if the workflow-level check were ever removed by mistake.

## 5. Fill in the ECR push policy template

Copy `ci/ecr-push-policy.json` to `ci/ecr-push-policy.local.json` and replace `<ACCOUNT_ID>` and
`<REGION>` with real values.

## 6. Create the role

```
aws iam create-role \
  --role-name CloudCopilotGithubActionsECRPush \
  --assume-role-policy-document file://ci/oidc-trust-policy.local.json \
  --description "GitHub Actions OIDC role for CloudCopilot's CI/CD pipeline to push images to ECR"

aws iam put-role-policy \
  --role-name CloudCopilotGithubActionsECRPush \
  --policy-name CloudCopilotECRPush \
  --policy-document file://ci/ecr-push-policy.local.json
```

## 7. Configure the GitHub repository

In the repo's Settings → Secrets and variables → Actions → Variables tab, add (these are
account-specific identifiers, not secrets, but still don't belong hardcoded in a committed
workflow file):

- `AWS_ACCOUNT_ID` — your account ID
- `AWS_REGION` — the region the ECR repositories were created in
- `AWS_ECR_PUSH_ROLE_ARN` — `arn:aws:iam::<ACCOUNT_ID>:role/CloudCopilotGithubActionsECRPush`

No access keys, no secrets — the whole point of OIDC is that GitHub Actions never holds a
long-lived AWS credential at all (see `docs/context.md` section 7.6).

## 8. Verify

Push a commit touching `CloudCopilot/**` to `main` and confirm the workflow's push-to-ECR job
succeeds, then confirm the images actually landed:

```
aws ecr describe-images --repository-name cloudcopilot-agent
aws ecr describe-images --repository-name cloudcopilot-audit-log
```
