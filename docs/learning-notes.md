# Learning Notes

Running log of concepts explained during this project, in the order they came up. Purpose: so
the reasoning behind decisions is still recoverable later, not just the decisions themselves.

## Session 1 — Architecture design (2026-08-03)

### Why IAM read-only doesn't make "let the LLM run arbitrary code" safe

The key mental model: **IAM scopes AWS API calls. It says nothing about code execution.**

If the fallback for "question isn't covered by our tools" is "have the LLM write a Python/boto3
snippet and `exec()` it," a read-only IAM role does not protect you — because the danger in that
design isn't "the LLM deletes an S3 bucket," it's "the LLM-authored code reads the container's
environment variables (which include your OpenAI key and AWS credentials), or opens an outbound
network connection to somewhere that isn't AWS at all, or just hangs the process." None of that
is an AWS API call, so IAM never gets a chance to block it.

The fix isn't "don't allow dynamic behavior" — it's "don't allow *code*." If the LLM's output is
restricted to `(service, operation, params)` and our own code is the only thing that ever calls
`getattr(client, operation)(**params)`, there's no code execution surface at all — just a normal
SDK call, the same as if we'd written that specific call by hand. *That's* the point where "IAM
is read-only, so the worst case is bounded" becomes true again.

### Why a prefix allow-list (`Describe*`/`List*`) isn't enough by itself

Naming conventions are a rule of thumb, not a security boundary. A few AWS operations match a
"safe-sounding" prefix but return sensitive content:

- `ec2:DescribeInstanceAttribute(Attribute=userData)` — a "Describe" call, but EC2 user-data
  often contains bootstrap scripts with embedded credentials/tokens.
- `logs:GetLogEvents` / `logs:FilterLogEvents` (CloudWatch Logs) — a "Get"/"Filter" call, but it
  returns raw application log content, which regularly contains personal data, stack traces, or
  leaked secrets.
- `ssm:GetParameter` on a plain `String` parameter (AWS Systems Manager's config-storage service,
  not marked as its encrypted `SecureString` type) — many teams store things like database URLs
  in plain, unencrypted parameters, so "only block the encrypted type" doesn't fully cover this.

Lesson: allow-lists for "safe" API access need to be curated per operation, not inferred from
naming patterns, however tempting the shortcut is. The prefix pattern is still useful as a first
coarse filter — it's just not sufficient on its own.

### Generated vs. hand-authored security artifacts

Early idea: auto-generate the IAM policy from the same config that drives the dispatch
allow-list, so they can never drift apart. Correct instinct (avoiding drift), wrong mechanism —
auto-generating *and applying* a security artifact means a bug in the generator (or an
unreviewed config change) can silently widen real AWS permissions with no human noticing.

Resolution: keep IAM hand-authored and reviewed like any other security-sensitive code change.
Keep the *drift-check* as an automated test instead of automated generation — the test only ever
fails a build (forcing a human to go look), it never writes to AWS or to the policy file. This is
a useful general pattern: automate the *check* that catches inconsistency, not the *authorship*
of the sensitive artifact itself.

### Why we're not persisting full AWS responses in the audit log

Read-only doesn't mean harmless-to-log. If a dynamic-dispatch call ever touches something with
sensitive content, and the audit log stores the full raw response, the audit database has just
become a second copy of that sensitive data — which now needs its own protection, undermining
the point of being careful about what the dispatch path can read in the first place. Logging
metadata (what was called, when, with what params, how many results, how long it took) instead
of full payloads keeps the audit trail useful for the "every action visible" requirement without
duplicating sensitive data at rest.

### Fixed tools vs. dynamic dispatch — the actual tradeoff

Not "flexible vs. safe," but "coverage vs. testability/predictability":

- Fixed, hand-written functions: fully unit-testable with mocked AWS responses, predictable
  output shape, can be hand-tuned to format answers nicely. Ceiling is "whatever we thought to
  implement."
- Structured dynamic dispatch: much broader coverage (any curated read operation across many
  services), but harder to test exhaustively (you test the *validation logic*, not every possible
  AWS response), and raw responses need more generic handling (pagination, size limits) since
  there's no hand-built formatting per operation.

Hybrid — fixed path first, dynamic dispatch as a fallback, both behind the same IAM backstop —
gets deterministic behavior for the common questions and graceful degradation for the long tail,
without picking one extreme.

## Session 2 — Phase 2 kickoff (2026-08-03)

### Least privilege should track what's running, not what's planned

Question going into Phase 2: does the IAM drift-check test have to wait until the fallback path's
allow-list is finalized (Phase 3b) before it can compare anything?

No — and the reason is a useful general habit, not just a one-off scheduling call. The five fixed
registry functions are self-describing: each one already names its exact AWS service + operation
(section 7.2). That's a real, enumerable list of AWS calls today, not a placeholder waiting on
future work. So there's no reason to either (a) stub out speculative fallback permissions now and
hope they're right later, or (b) block Phase 2 on Phase 3b finishing.

The general lesson: a least-privilege policy's scope should always match what the software
actually does *right now*, not what it's designed to eventually do. Granting permissions ahead of
the code that will use them means there's a window where AWS trusts the role with more than the
running system needs — exactly the kind of unused, speculative privilege the whole "least
privilege" principle exists to avoid. It's better to extend the policy (and its drift-check) in
the same PR that lands the feature needing it, than to pre-grant it and hope the two stay in sync.

### The same "safe-sounding name isn't safe" lesson, applied to IAM action strings themselves

Session 1 already established that a `Describe*`/`List*` *prefix* isn't a safe filter for what an
operation *returns* (see above). Writing the actual IAM policy surfaced a second, narrower version
of the same lesson: the IAM action *name* isn't even reliably derivable from the boto3 method name
by mechanical string conversion, for some services.

Concretely: boto3's `list_buckets()` call maps to the S3 `ListBuckets` API, and a naive
snake_case → PascalCase conversion would produce the action string `s3:ListBuckets`. That string
doesn't exist. The actual IAM action authorizing that call is `s3:ListAllMyBuckets` — a legacy
name that predates a naming-convention cleanup elsewhere in the S3 API. Similarly, `s3.get_public_
access_block()` is authorized by `s3:GetBucketPublicAccessBlock`, not `s3:GetPublicAccessBlock`.
Confirmed against AWS's Service Authorization Reference rather than assumed.

Consequence for the registry's design: `AwsCall.iam_action` is its own explicit, hand-written
field — not a derived property computed from `service` + `operation`. If it were derived, this
class of mismatch would either silently produce a policy that fails at runtime (action doesn't
exist) or, worse in a differently-shaped bug, silently grant a *different*, unintended action that
happens to share a name pattern. Curate, don't infer — the same rule as the fallback allow-list,
just showing up one level lower, in policy authorship instead of dispatch validation.

### Deferring dynamic dispatch: telling a spec requirement apart from a self-imposed ambition

Decided to postpone structured dynamic dispatch (7.2 tier 2) until after a working base version
ships, replacing it in v1 with a suggestion-only fallback (no AWS call, just plain-language
pointers for legitimately AWS-shaped questions). Worth recording *how* that call got made, since
the same test is reusable later: go back to `mission-spec.md` and check whether the thing being
deferred was ever actually required, or whether it was an ambition added on top during
architecture design. Here, the spec's actual bar is "5+ AWS operations" and "10 questions answered
correctly" — both satisfiable by the fixed registry alone. Dynamic dispatch was Session 1's own
idea for making the tool feel more conversational, not something asked for. Deferring a
self-imposed ambition to ship a solid base first is a very different move from deferring an actual
deliverable — the former is healthy sequencing, the latter would need to be flagged as a real risk.

A nice side effect of the deferral: the IAM policy's scope stays exactly what it is today (the
registry's 7 actions) for as long as dynamic dispatch doesn't exist, instead of needing the
"somewhat broader than 5 actions, to cover the fallback path" widening that 7.2 originally
described. Sequencing the harder, more security-sensitive piece *after* the simpler piece is
solid isn't just lower-risk work ordering — it directly produces a smaller, easier-to-justify IAM
policy for however long v1 lives without dynamic dispatch.

### Why a soft, AI-judgment-only guardrail is acceptable in exactly one place

Everywhere else in this design, safety comes from IAM and our own code deciding what's allowed —
explicitly *not* the AI's own judgment (see the top of this file). The suggestion-only fallback
breaks that pattern: the AI's own sense of "is this a legitimate question" and "is this suggestion
safe to give" is the only guardrail there. That looks like a departure from the project's own
stated principle, but it isn't, once you notice *why* it's still acceptable: this path never
touches the real AWS account at all. There's no live data for the AI to leak, because it was never
given any. The worst case is generic bad advice, not exposure of this account's actual resources.
The "trust the AI's judgment" pattern is exactly as risky as the project has been avoiding
elsewhere *when there's real data behind it* — it's only safe here because there deliberately
isn't any.

## Session 2 — Phase 3: pagination + unit tests (2026-08-04)

### What moto actually is, and where its realism runs out

`moto` is a library that intercepts boto3's calls before they leave the machine and answers them
from an in-memory fake AWS account instead of the real network. Create a fake EC2 instance inside
a `@mock_aws`-wrapped test, and moto remembers it — a later `describe_instances()` call in that
same test returns it, shaped exactly like a real AWS response (same keys, same nesting). That's
what makes it useful: it exercises the *real* boto3 parsing and error-handling code paths, with no
real account, no credentials, no cost, and no network latency.

Its fidelity isn't uniform, though. EC2, S3, and IAM are deeply modeled — real create/list state,
faithfully tracked. Cost Explorer is not: moto doesn't compute costs from resource usage at all,
`get_cost_and_usage` just returns empty by default. Testing `cost_by_service_this_month`'s actual
parsing logic needed a different tool — `botocore.stub.Stubber`, which lets a test hand-craft an
exact API response and assert the function parses it correctly, without depending on any AWS
simulation at all. Lesson: don't assume a mocking library's coverage is uniform across services —
check the specific operation you need before designing tests around it.

### A session/region mismatch that looked like a code bug and wasn't

While verifying pagination changes, `ec2_uptime()` returned an empty instance list even though a
fake instance had just been created under moto. The pagination logic wasn't the problem — the
verification script was. It created the fake instance using a client explicitly scoped to
`us-east-1`, but then called `ec2_uptime()` with no `session` argument, which made the function
build its *own* client from boto3's ambient default region — a different region than the one the
fake resources lived in. moto keeps each region's fake state separate, so the "other" region's
mock backend legitimately had nothing in it.

The fix was procedural, not a code change: every test (and this verification script) needs to
create fixtures and call the function under test using the *same* explicit, region-scoped
`session`. This is exactly why each registry function accepts an optional `session` parameter in
the first place — it's dependency injection specifically so tests can control this instead of
relying on ambient environment state.

### Pagination: a real gap found by re-reading the code, not by a failing test

While reviewing `registry.py` before writing tests, none of the five functions handled paginated
AWS responses — each made one API call and treated whatever came back as complete. Checked against
botocore's paginator registry: four of the five operations actually do paginate
(`DescribeInstances`, `DescribeSecurityGroups`, `ListBuckets`, `ListRoles`/`ListUsers`); only
`GetCostAndUsage` doesn't have a boto3-native paginator (it has its own manual `NextPageToken`
field instead). In a small sandbox account this gap was unlikely to ever actually trigger — but
"unlikely to matter in the demo" and "actually correct" are different bars, and section 7.2 already
treats exactly this failure mode (silently truncated results reported as complete) as unacceptable
for the fallback path. Fixed by routing the four paginating calls through boto3's
`get_paginator(...).paginate().build_full_result()`, and adding a hand-written loop for Cost
Explorer's manual token. Tested the pagination-following logic itself once, in isolation, with
`Stubber` supplying two linked pages — since all four paginated calls share one helper
(`_paginated`), that one test covers the merging logic for all of them, rather than repeating a
multi-page setup in every individual capability's test.

## Session 2 — Phase 4: LLM routing, audit granularity, and CLI (2026-08-04)

### A registry designed as "self-describing" pays off again, in a place it wasn't built for

`RegistryEntry.name`/`description`/`params_schema` were originally designed (Session 1) just to
drive the AI's menu and the CLI's help text. When it came time to actually wire up OpenAI's native
tool-calling, those same three fields turned out to map onto the API's tool schema
(`name`/`description`/`parameters`) almost exactly, with zero redesign. Worth noticing as a
general pattern: describing something honestly and completely once, for one purpose, often makes
it reusable for a second purpose nobody had in mind yet — the alternative (special-purpose,
narrowly-shaped data for each consumer) would have meant keeping the AI's tool list and the
registry in sync by hand.

### "Log every AWS call" has a hidden granularity question, and pagination is the wrong axis to split on

When wiring up per-call audit logging, the natural instinct was to log once per real network
request. But that's not quite right either: `_paginated`'s multi-page fetches are real, distinct
HTTP requests to AWS, and logging one entry per page would be technically defensible — yet it
would fragment the audit trail along a distinction ("how many pages did AWS need to hand back all
the data") that has nothing to do with *what our code decided to do*. The dividing line that
actually matters is decision-based, not request-based: one audit entry per logical operation our
code chose to perform, where pagination (boto3-native or hand-rolled) is a mechanical continuation
of one decision, but calling the same operation again for a *different resource*
(`get_public_access_block` once per bucket) is a genuinely separate decision each time. Getting
this distinction right mattered enough to change the actual code shape — `audited_call` (one
real call, audited once) and `_paginated` (many real calls, audited as one) are deliberately
different functions rather than one function with a "how many times to log" flag.

### Keeping a security-relevant module ignorant of the thing it's helping secure

`agent/registry.py`'s `audited_call` never imports or knows about `AuditLog`/`AuditEvent` — it
only ever calls a bare, generically-typed callback: `(service, operation, params, success,
result_count, duration_seconds) -> None`. All of the *meaning* of that callback (what an "audit
log" is, what a `question_id` is, how it gets persisted) lives entirely in `agent/router.py` and
`agent/audit.py`. This wasn't just tidiness — it's what let every Phase 3 test keep working
completely unchanged (they call handlers with no `audit` argument at all, which defaults to doing
nothing), and it means `registry.py` — the module every reviewer will scrutinize hardest for
security correctness — has no dependency on, or opinion about, a concern (logging/persistence)
that isn't actually its job.

### An AWS-level "failure" can be exactly the informative signal, not noise

While verifying `audited_call`'s behavior against real moto responses, two of three
`get_public_access_block` calls came back as `success: False` in the audit log — even though
`s3_public_access`'s actual answer to the user was completely correct. The raw AWS call genuinely
did return an error (`NoSuchPublicAccessBlockConfiguration`) for buckets with no configuration set;
our own handler code interprets that specific error as meaningful business data ("not blocked"),
but the audit log's job is to record what actually happened at the AWS API level, not our
downstream interpretation of it. Recording the raw truth is the more honest choice — and it's the
same principle 7.7 already applies to AWS `AccessDenied` responses: a refusal from AWS itself is
informative, not evidence that something is broken.

## Session 2 — real usage finds what unit tests can't (2026-08-04)

### Actually asking the CLI a question found bugs 44 passing tests hadn't

Unit tests proved the *logic* was correct for the inputs we thought to test. Actually running the
CLI with a real question found gaps those tests never could, because the tests were only ever as
good as the scenarios imagined for them: a capability with a real, silent hole in what it covers
(IAM groups), and a question whose *framing* ("more than 24 hours") didn't map onto any actual
parameter, causing the model's routing decision to become genuinely inconsistent between
identical-ish asks. No unit test would have caught either, because both tests would have had to
already know to ask about the thing that was missing. Lesson: a thoroughly unit-tested system
still needs to actually be *used*, by someone thinking like a real asker, before its interface
gaps become visible — tests validate what you built, not what you forgot to build.

### The one-call design has a subtle consequence for honesty about partial matches

Once a tool is called, its formatted result *is* the final answer — there's no second model turn
to add "by the way, I couldn't cover X." That was a deliberate trade for cost/speed/determinism
(learning-notes.md, Session 2's earlier note on formatting), but it means any fix for "the model
silently answered only part of a compound question" has to happen at the *routing* decision (call
vs. don't call a given tool), never as a disclosure appended after the call. Trying to fix this by
telling the model to "mention what's missing" after calling a tool would have been asking for
something the architecture doesn't have room for. The actual fix had to be a rule about when *not*
to call a tool at all.

### An ambient real credential on the dev machine is a live risk, not a hypothetical one

While adding tests for the EC2 threshold fix, one new test was missing `@mock_aws` — a plain
authoring mistake. It didn't fail; it silently made a real `DescribeInstances` call against a real,
unrelated AWS identity (a corporate SSO role tied to this machine's `~/.aws/config`, entirely
separate from the sandbox `.env` used for the intentional dry run) and only surfaced because that
identity happened to have an explicit deny on the action. Had it been allowed, the mistake would
have been invisible. The fix wasn't "remember the decorator next time" — it was to make the
mistake structurally unable to reach real AWS at all: an autouse pytest fixture
(`tests/conftest.py`) that forces obviously-fake credentials for every test in the suite, so a
missing `@mock_aws` now fails loudly (`AuthFailure`) instead of silently succeeding against
whatever real credentials happen to be sitting on the machine. General lesson: don't rely on every
test author remembering an isolation step correctly every time — make the unsafe state
structurally unreachable instead.

## Session 2 — Phase 5: building a real service without Docker yet (2026-08-04)

### The same dependency-injection pattern, reused a third time

`registry.py`'s handlers take an optional `session`; `router.py`'s `answer_question` takes an
optional `openai_client`; `HttpAuditLog` now takes an optional `client`. All three exist for the
same reason: a class/function that talks to something external needs a seam where a test (or a
different runtime environment) can swap in something else — a moto-backed session, a fake OpenAI
response, an `httpx.Client` wired to a `MockTransport` — without the production code path knowing
or caring. Once this pattern shows up a third time in the same codebase, it's worth naming
explicitly: "does this thing make an external call?" should almost automatically prompt "does it
accept the client/session as a parameter, defaulting to the real one?"

### Proving app and client work together *before* Docker exists, not after

Rather than write the FastAPI service and its client and only find out if they actually agree on
the wire format once Docker Compose was wired up, the service was run directly with `uvicorn` on
plain `localhost`, and `HttpAuditLog` was pointed at it for a real end-to-end check — actual HTTP
requests, actual SQLite writes, actual JSON round-tripping. This caught nothing new by the time it
ran (the unit tests had already covered the contract), which is itself the useful signal: it
confirmed the pieces genuinely fit together, rather than each being correct in isolation but never
verified against each other. Deferring Docker to its own phase only works as a *simplification* if
the thing being containerized already demonstrably works — otherwise "the container doesn't work"
becomes an ambiguous bug report that could mean the app is wrong, the Dockerfile is wrong, or the
network config is wrong, all at once.

### An audit-log service failing to record must never look like the agent failing to answer

`HttpAuditLog.record` catches its own HTTP errors and prints a warning rather than raising. This
was a deliberate choice, not an oversight: the audit trail matters, but it is not the thing the
user actually asked for. If logging one action failed and that exception propagated up through
`router.py`, a user's real, successfully-answered question would come back as an error purely
because a *side effect* of answering it didn't get recorded. Keep the failure domains separate —
"did we answer the question" and "did we manage to log that we answered it" are different
questions with different acceptable failure modes.

## Session 2 — Docker, step 1: the audit-log service's Dockerfile (2026-08-04)

### `COPY` happens as root; `USER` switches after — ownership has to be handed over explicitly

The Dockerfile copies app code, then creates an unprivileged user and switches to it with `USER`.
The first build crashed the moment the container actually ran: `sqlite3.OperationalError: unable
to open database file`. Not a code bug — a permissions one. `COPY` executes before `USER` takes
effect, so the copied files end up owned by `root`. The app then runs as `appuser` and tries to
*create* a new file (the SQLite database doesn't exist yet) inside a directory it doesn't own —
permission denied, surfacing as a confusing-looking "unable to open database file" rather than an
explicit permissions error. Fix: `chown -R appuser:appuser /app` before the `USER` switch. General
lesson: in a multi-user-aware Dockerfile, ownership doesn't follow the `USER` instruction
automatically — anything copied in earlier stays owned by whoever ran that `COPY`, and has to be
explicitly handed over if the later, unprivileged user needs to write to it.

### `--rm` deletes the evidence exactly when you need it most

The first debugging attempt used `docker run --rm ...`, which deletes the container the instant it
stops — including its logs. The container crashed immediately, and there was nothing left to
inspect. Re-running without `--rm` (then `docker logs <container>`) revealed the actual traceback.
General lesson: `--rm` is the right default for a container you expect to work — convenient
auto-cleanup — but it actively destroys the one thing you need while diagnosing a container that
*doesn't* work. Drop it while debugging, add it back once things are stable.

### A server binding to `127.0.0.1` inside a container is unreachable from outside it

The `CMD` explicitly passes `--host 0.0.0.0` to uvicorn. Left at its default (`127.0.0.1`), the
server would only accept connections from *within its own container's network namespace* — not
from the host machine via a published port, and not from another container on the same Docker
network later. This is one of the most common "works locally, broken in a container" bugs, and it
never announces itself as an error — the server starts up fine and just never receives the
requests, since Docker's port publishing/networking routes traffic to the container's network
interface, not to loopback specifically.

### `.dockerignore` patterns need an explicit `**/` prefix to match below the root

Without a `.dockerignore` at all, the built image picked up `__pycache__/`, a stale local
`events.jsonl` audit file, and the entire `tests/` directory — none of which belong in a runtime
image. Adding one with `__pycache__/` and `*.jsonl` (no `**/` prefix) didn't fix it — those patterns
only match at the build context's *root*, not one level down inside `audit_log/`, where these
files actually live. `**/tests/` worked immediately, because it already had the depth-matching
prefix. Verified this empirically (build, inspect the image's contents, adjust, rebuild) rather
than trusting the first "looks right" version — a `.dockerignore` that silently fails to exclude
anything is easy to not notice, since a build with too much context still succeeds, it's just
larger and messier than it should be.

## Session 2 — Docker, step 2: the agent's Dockerfile (2026-08-04)

### `ENTRYPOINT` vs `CMD` matters once the thing being containerized isn't a server

Step 1's service always runs the same way — start it, it listens forever. The agent doesn't: it
either answers one question and exits, or loops reading from stdin. `ENTRYPOINT
["python", "-m", "agent"]` (not `CMD`) is what makes this map cleanly onto Docker: anything typed
after the image name in `docker run` becomes an argument to that fixed command, so
`docker run <image> "<question>"` and `docker run -it <image>` naturally reproduce the CLI's own
argv-based branching without writing any container-specific logic at all. The general lesson: pick
`ENTRYPOINT` vs `CMD` based on whether the thing being wrapped is inherently "always run this exact
way" (a server) or "run this, but let the caller supply what varies" (a CLI/tool).

### Proving secrets aren't baked in means testing the failure case, not just the success case

Running the built image with zero environment variables passed failed with a clear "missing
`OPENAI_API_KEY`" error. That failure *is* the actual proof the "no secrets baked into layers"
requirement holds — if the same command had somehow produced a real answer with no credentials
supplied, that would mean a key had leaked into the image some other way (a stray `.env` copy, a
hardcoded default, a leftover build arg). A security property phrased as "X doesn't happen" is best
verified by deliberately trying to make X happen and confirming it doesn't, not by only testing the
happy path.

### A read-only volume mount can break credential providers that need to write, not just read

Mounting `~/.aws` into the container to resolve the SSO profile failed with `Read-only file
system`, even though the intent was "let the container *read* my AWS config." boto3's SSO
credential provider isn't purely read-only — refreshing a cached token means writing the refreshed
value back to `~/.aws/sso/cache/`, and a `:ro` mount blocks that write just as much as it would
block an intentional edit. General lesson: "should this mount be read-only" depends on what the
*consumer* actually does with the files, not just on what you intend for it to do — a credential
provider or any similar library can have write-as-part-of-reading behavior (caching, refreshing,
locking) that isn't obvious from the outside.

## Session 2 — Docker, step 3: docker-compose.yml (2026-08-04)

### A volume mounted at a code directory replaces the code, it doesn't merge with it

Before writing any compose config, working through *why* a persistent volume for the SQLite file
couldn't just point at `/app/audit_log` (where the code already lives) surfaced something
non-obvious: a volume mount doesn't overlay or merge with what the image already put at that path
— it *replaces* it. An (initially empty) named volume mounted at `/app/audit_log` would make
`app.py` and `storage.py` simply disappear from the container's point of view, since the volume's
contents now stand in place of whatever the image had there. The fix was moving the database to
its own directory (`/data`) with nothing else in it, specifically so a volume could own that
location without colliding with anything the image needs to keep. General lesson: "where does this
data live" and "where does this code live" need to be different paths the moment either one needs
independent lifecycle management (a volume that persists across container recreation, code that
gets replaced on every rebuild).

### `profiles` distinguish "a service you leave running" from "a tool you run once"

Every service defined in a compose file gets started by a bare `docker compose up` unless told
otherwise. That's correct for `audit-log` (a daemon, meant to keep running) and wrong for `agent`
(a CLI tool, meant to run once per invocation and produce output) — starting it as a "service"
with no real terminal attached would just leave it hanging or exiting immediately. `profiles:
["cli"]` opts a service out of the default `up` while keeping it fully usable via `docker compose
run --rm agent "<question>"`. General lesson: not everything that's *defined* in a compose file is
a "service" in the "long-running thing" sense — Compose's profile mechanism exists because that
distinction is common enough to need first-class support, not something to work around.

### The most common Docker mistake there is: forgetting to rebuild after a code change

The agent's audit-call failures simply weren't showing up in the audit trail at all, and hours were
spent seriously investigating the audit-logging mechanism itself for a bug — checking whether
`HttpAuditLog` really posted correctly, whether the callback chain in `registry.py`/`router.py` was
sound, reproducing the exact failure path piece by piece. Every one of those pieces turned out to
work perfectly in isolation. The actual cause: `agent/cli.py` had been edited (adding
`_default_audit_log()`) *after* the agent's image was last built, and the running container was
still executing the old, pre-edit code — which had no `AUDIT_LOG_URL` awareness at all, and just
silently used `FileAuditLog` writing into the throwaway container's own filesystem. A Docker image
is a frozen snapshot of the filesystem at build time; editing source files on the host has zero
effect on a container already running from an image built before that edit — `docker build` has to
happen again. This is, by a wide margin, the single most common way to convince yourself a fixed
piece of code still doesn't work: you're not looking at the code you think you're looking at.
General lesson, and a genuinely useful debugging habit: when something that should obviously work
doesn't, check "did I actually rebuild since the last change" *before* re-investigating the logic
itself from scratch.

## Session 2 — building the CI/CD pipeline (2026-08-05)

### A security gate that can never pass is worse than no gate

Trivy's first run against the freshly-built images returned 23 HIGH/CRITICAL findings — every one
an OS-level Debian package CVE inherited from the base image, none caused by this project's own
code. The instinct might be to treat "fail on any HIGH/CRITICAL" as the obviously-correct, most
secure reading of the mission's requirement. Checking each finding's actual fixed-version field
told a different story: zero of the 23 had a fix available anywhere yet. Gating on them literally
would mean the build fails forever, unconditionally, with nothing anyone could do about it right
now — and a permanently-red pipeline doesn't make anyone more careful, it just teaches everyone to
stop looking at CI results at all, which is strictly worse than not having the gate. The fix,
`--ignore-unfixed`, isn't a weaker gate — it's a gate that only fires on things actually within
this project's control to fix, which is what "the pipeline should fail when something's wrong"
actually needs to mean in practice.

### Verifying a suppression comment "worked" means re-running the scanner, not reading the comment

A Semgrep false positive got a `# nosemgrep: <rule-id>` comment added — placed, reasonably, as its
own explanatory line directly above the flagged code. Re-running Semgrep showed the exact same
finding, completely unsuppressed. The comment had to be on the *exact* line Semgrep reports as the
finding, not a preceding line, however closely associated. This is a small, specific mechanical
fact about one tool, but the general habit it reinforces is bigger: a fix that "looks like it
should work" (a plausible comment, in a plausible place, referencing the right rule ID) still needs
its actual effect checked directly — re-run the tool, confirm zero findings — rather than trusting
that writing the fix correctly-looking is the same as the fix taking effect.

### Distinguishing "the tool has a known limitation" from "what I wrote is wrong"

Testing the workflow with `act` produced failures in two different actions that use JavaScript
internally (`setup-python`'s cache-save step, `aws-actions/configure-aws-credentials`), both with
the identical error: `node: executable file not found in $PATH`. It would have been easy to read
this as "the workflow is misconfigured" and start second-guessing the YAML. Two direct checks
resolved it instead: the error message itself linked a specific, already-known upstream `act`
issue, and a direct check (`docker run --rm <act-runner-image> find / -iname node`) confirmed
Node.js genuinely exists inside the runner image, just not on the `PATH` `act` expects for that
one internal invocation. That's a fact about *act's own environment simulation*, not about
anything in this project. The discipline worth naming: when a tool fails in a way that seems
disconnected from the actual change you made, check whether the tool itself has a known, external
explanation before assuming the fault is in your own work — and check it directly (read the linked
issue, inspect the image) rather than guessing either way.

### When a full local run isn't possible, verify every piece you can, honestly, and name what's left

`act` couldn't run the complete pipeline end-to-end locally — partly its own Node.js limitation,
partly because the OIDC/ECR push step genuinely requires real AWS infrastructure that doesn't
exist yet. Rather than treat "can't fully verify" as "can't verify anything," each job's
substantive steps got checked directly and separately: `ruff check`/`ruff format` confirmed passing
from `act`'s own output before its unrelated post-step failure; the coverage gate's actual
pass/fail behavior tested directly at the command line instead of through `act`; a temporary,
uncommitted, `needs:`-stripped copy of the workflow used specifically to run the build/Semgrep/
Trivy job in isolation, which confirmed all of it genuinely works. What's left unverified — the
AWS push — is named explicitly as such, not glossed over. Partial verification, done honestly and
with the gap clearly labeled, is worth much more than either skipping verification entirely or
quietly implying more confidence than what was actually checked.

## Session 2 — building structured dynamic dispatch (2026-08-05)

### Verifying an allow-list entry means reading the service model, not guessing from the name

Adding ELBv2 to the dispatch allow-list surfaced a real trap: boto3's client name for it is
`elbv2`, so it would be easy to assume its IAM actions use an `elbv2:` prefix too. AWS's Service
Authorization Reference says otherwise — the actual prefix is `elasticloadbalancing:`, a holdover
from when there was only one load balancer product. Every other service added this round
(`cloudwatch`, `ec2`, `rds`, `autoscaling`) does have its boto3 client name match its IAM prefix
exactly, which makes ELBv2 easy to miss if you check the pattern on a couple of services and
assume it generalizes. Caught only because the drift-check test (`tests/test_iam_drift.py`)
compares `iam_action` strings, not client names — a wrong prefix there would have failed the test
immediately once the IAM policy was written against the correct name, not the guessed one. Lesson
carried over from Session 1's "naming patterns aren't a security boundary" point: it applies just
as much to *verifying* an allow-list as to deciding what belongs on one.

### A blocked action is still an action — it belongs in the audit log too

The first cut of `dispatch.run_curated_call` only called the audit callback on success or on an
AWS-side failure. A request for a denylisted operation (say, `secretsmanager:GetSecretValue`)
would raise `DispatchNotAllowed` and return — with nothing recorded anywhere. That's backwards
given the mission's own framing: "every action visible" is exactly the property an audit log
exists to guarantee, and a blocked attempt is arguably *more* interesting to have visible than a
routine successful one — it's the moment the safety net actually did something. Fixed by auditing
the block itself, tagged `success=False`, before raising. The general shape worth keeping: when a
safety check exists specifically to *prevent* something, the fact that it fired is itself data,
not noise to discard once the prevention succeeds.

### Compound tasks need more prompt scaffolding than single-decision ones, even when both are "supported"

The registry's `min_uptime_hours` parameter and dispatch's `sort_by`/`sort_descending`/`limit`
shaping are the same underlying feature — let the AI ask for a specific slice of data instead of
dumping everything — but they turned out not to be equally reliable in practice. Asking the
registry's EC2 tool to filter by uptime only requires the model to notice "this question has a
number in it, pass it as the parameter" — the parameter's existence and name are already given.
Asking dispatch to shape a result requires two things at once: *deciding* that shaping is needed at
all (not spelled out per-operation, since dispatch is generic across every allow-listed call), and
*guessing the exact real AWS field name* to sort by (e.g. `InstanceCreateTime`, not `CreateTime` or
`CreationDate`) with no schema to check it against. A first end-to-end smoke test (real OpenAI,
moto-mocked AWS) confirmed this gap directly: "which of my RDS instances is the newest?" correctly
triggered dispatch, but the model returned both raw DB instances instead of narrowing to one. The
existing `SYSTEM_PROMPT` line mentioning `sort_by`/`limit` was a single passive sentence buried at
the end of a long prompt — true, but not load-bearing enough for a two-part inference. Rewriting it
as an imperative rule ("you MUST include `sort_by` and `limit`") with one fully worked example
using this exact question and the real field name fixed it, verified by re-running the same smoke
test and confirming the model now returns exactly one result. General lesson: whether a capability
is *unit-tested* and whether a capability is *reliably invoked by the model in a realistic
question* are genuinely different things to verify — the first only proves the mechanism works
when told what to do; the second proves the prompt actually tells it to, for the compound case, not
just the simple one.

## Session 2 — optimizing the CI/CD pipeline (2026-08-05)

### A job dependency should reflect a real data/ordering need, not just "where the step happened to be written"

Semgrep had been living inside `build-scan-push`, gated behind `needs: test`, purely because that's
where it got added originally — not because it actually needed anything `test` or the Docker build
produced. It only ever reads source files. Once actually asked "does this step's position reflect a
real dependency," the answer was no, and moving it to its own job (`needs: lint`, running alongside
`test`) shortened the pipeline for free. The general check worth applying anywhere a `needs:` chain
exists: for each edge, name the actual artifact or guarantee that makes the dependency real — if you
can't name one, the ordering is probably just historical, not necessary.

### Why splitting build/scan/push further wasn't worth it (yet)

The same "make it more granular" instinct that correctly caught the Semgrep placement doesn't
automatically apply everywhere. Build, Trivy-scan, and push currently share one job specifically so
the Docker images built in step 1 are still sitting in that job's own VM when later steps need them.
Splitting them into separate jobs is possible, but GitHub Actions jobs each get a fresh VM — nothing
carries over automatically, so the images would need `docker save` + `actions/upload-artifact` out
of one job and `download-artifact` + `docker load` into the next. That's a real, known pattern, not
a blocker — but it's overhead with a cost (upload/download time on every run) purely for visibility
in the Actions UI, no behavioral change. Decided to skip it for now. The lesson isn't "granularity is
bad," it's that "more granular" has to be weighed against what crossing that boundary actually costs
— cheap for Semgrep (no shared state to move), not free for Docker images.

### A temporary security downgrade is fine when it's named as one, in the place someone will actually see it

Swapping OIDC for static SSO session credentials to unblock testing is a real regression — GitHub
secrets are a standing value that only expires if you remember to rotate it, versus OIDC's
per-run, auto-expiring token. The mitigating factors that make this an acceptable *temporary* move
rather than a quiet downgrade: it's blocked on the same, already-documented `iam:CreateRole` gap as
the permanent design (not a new problem, an existing one applied to a new spot); the credentials
themselves are still short-lived SSO session tokens, not permanent IAM user keys, so the "damage
window" if one leaked is naturally bounded to hours, not indefinite; and — the part actually worth
generalizing — the workflow file itself carries a comment explaining exactly why this exists, that
it's not the intended permanent mechanism, and what to revert to and when. A security tradeoff
documented only in a chat conversation or a person's memory effectively isn't documented at all six
months later; putting the "this is temporary, here's why, here's the revert path" note directly in
the file most likely to be read when someone eventually asks "wait, why are we doing this" is what
actually makes a shortcut like this safe to take.

## Session 3 — a provided role that doesn't match what the code needs (2026-08-06)

### "It's called read-only" isn't the same claim as "it grants what we need"

Getting an actual role ARN felt like it should close out section 8's blocker outright. Comparing
its attached policy action-by-action against what the registry and dispatch code actually use
turned up something more interesting: only about a third of the actions overlapped. The rest split
in both directions — several things our own registry depends on (`ce:GetCostAndUsage` — an entire
capability, and the mission spec's own named example question — plus `iam:ListUsers`/`ListGroups`
and `s3:GetBucketPublicAccessBlock`) simply aren't granted, while other things we'd never built for
(EC2 networking metadata, EKS, S3 object listing, CloudWatch Logs filtering) are. Neither list was
a subset of the other. The lesson: "is this role read-only" and "does this role grant what my code
needs" are two entirely different questions, and answering the first (yes, structurally, by
inspection) tells you nothing about the second — that has to be checked directly, action by action,
the same discipline this project has already applied to naming patterns and IAM action strings.

### When the permissions boundary can't be fixed, move the enforcement into the code that's actually reviewable

The original design's IAM backstop assumed we could always shape the deployed role to match our
code's curated allow-list exactly (that's what the drift-check enforces for `iam/policy.json`).
Here that assumption broke: the role is provided externally and isn't negotiable on a reasonable
timeline. Rather than treat "the policy doesn't match reality" as something to paper over, it became
two separate, honestly-named documents — `iam/policy.json` stays what it always was (our own ideal
ask), and a new `iam/granted-actions.json` records the external fact of what's actually enforced.
The actual safety property people would care about — "does this agent only try things it should" —
now lives in a runtime check (`agent/permissions.py`) instead of solely in IAM. That's a real
philosophical shift worth naming: normally this project's answer to "what if our code has a bug" is
"IAM would stop it anyway" (learning-notes, Session 1). Here IAM is *wider* than intended, so that
backstop is weaker for the specific actions it over-grants (`logs:FilterLogEvents`, the ECR write
actions) — the honest fix isn't pretending otherwise, it's writing that gap down explicitly
(context.md section 8) and making sure the code-level checks (the deny-list, the count-only
stripping, the "never call ecr:* at all" fact) don't depend on IAM to hold the line there.

### Two shapes of "looks risky by name" need two different kinds of mitigation, not one

`s3:ListBucket` and `logs:FilterLogEvents` both got flagged as risky in the same conversation, but
they don't call for the same fix, and treating them identically would have been a mistake. S3 keys
are a *bounded, structured* field — a filename, checkable against a finite list of suspicious
substrings, with a clear, nameable ceiling on what leaks (a filename, never contents, since
`GetObject` stays denied). Log messages are *unbounded, free text* with no schema at all — a
substring/regex filter over that has an unknown, unboundable false-negative rate; it would catch
known-shaped secrets and miss everything else, including exactly the kind of sensitive content
(PII, internal architecture in a stack trace) this project's whole design has been trying to avoid
exposing. The actual fix had to match the actual shape of the risk: flag-and-disclose for the
bounded case, don't-expose-the-content-at-all (count-only) for the unbounded one. "Add a safety
mechanism" isn't one move — it's a different move depending on whether the risky thing is a finite
field or open-ended text, and mistaking one for the other would have meant either over-restricting
a fairly safe capability or under-restricting a genuinely risky one.

### A drift-check test can lock in a known gap on purpose, not just an ideal state

Every other test in this project's IAM story (`tests/test_iam_drift.py`) asserts two things *should*
match. `tests/test_permissions.py`'s regression test does something slightly different: it asserts
the current *mismatch* — the exact set of actions the deployed role doesn't grant — matching what
`context.md` claims today. That's deliberately a test of a gap, not an ideal. The value is the same
kind of forcing function as any other drift-check: if the real role's grant changes, or the code's
own action set changes, this test breaks and makes someone go update both the code's understanding
and the docs together, instead of the two silently drifting apart the moment either side changes
without the other noticing.

### "Blocked on X" can quietly mean "blocked on X without a required argument," not "blocked, period"

`docs/context.md` had said for two sessions that the sandbox identity lacked `iam:CreateRole`
outright. Trying to create the CI/CD pipeline's push role through the AWS console got `AccessDenied`
— consistent with that belief. But the actual CLI command a mentor provided succeeded, using the
exact same underlying permission, just with one more argument: `--permissions-boundary`. The
identity was never denied `iam:CreateRole` itself; it was denied *any* `CreateRole` call that didn't
specify a permissions boundary — almost certainly an IAM policy condition requiring
`iam:PermissionsBoundary` to be set, a common guardrail for exactly this kind of delegated,
safer-by-construction self-service role creation. The console's create-role flow doesn't expose that
field in the same request, so it could never have succeeded there regardless of what the identity
could actually do. The lesson: a denied action's error doesn't distinguish "you don't have this
permission" from "you have this permission, but not in the shape you just tried it in" — when a
permission looks like it should exist for other reasons (a mentor explicitly pointing at a working
command, an org that clearly expects interns to provision their own scoped roles), it's worth
checking for a narrower, gated path before concluding the block is absolute.

### A new module's import-time file read is a new dependency the Docker build has to know about

`agent/permissions.py` reads `iam/granted-actions.json` the moment it's imported — correct and
deliberate (fail fast if the file's missing, rather than at the first question asked). What wasn't
obvious until the container was actually run: adding that read created a real dependency from the
`agent` package on a sibling directory, and `agent/Dockerfile` only ever copied `agent/` itself.
Every pytest run exercises this from the repo checkout, where `iam/` is right there next to
`agent/` — so nothing in the automated suite could ever have caught this, no matter how much
coverage it had. This is the same category of gap the original Docker-phase sessions already found
(forgetting to rebuild, `.dockerignore` patterns, `chown` before `USER`) — a containerized app's
actual dependency surface is only fully known by running the built image, not by testing the code
that runs happily from a full checkout. General lesson to carry forward: any time a new module
reads a file by a repo-relative path, ask "does the Dockerfile actually copy this in" before
assuming it does, the same way `COPY agent/` was never assumed to imply anything about `iam/`.

### Diagnosing a live-model routing bug means calling the real API, not reading the prompt and guessing

A question ("what VPCs exist") got a confidently wrong answer — an EC2 instance list — instead of
either the right dispatch call or a decline. It would have been easy to just reread `SYSTEM_PROMPT`,
spot something that looked underspecified, tweak it, and move on. Instead, the actual OpenAI API was
called directly with the real tool schemas, isolated from everything else (no AWS, no Docker), to
see exactly what the model does today — confirmed a real, reproducible mismatch (4/4 wrong), not a
one-off. Then a candidate fix (one sentence added to `ec2_uptime`'s description) was tested the exact
same way, live against the real API, *before* touching the committed file — 5/5 correct. Only after
that did the change get made to `agent/registry.py`, and only after rebuilding the actual container
was it confirmed the fix holds end-to-end. This mirrors a lesson from Session 2's dispatch work
("verifying a suppression comment worked means re-running the scanner, not reading the comment") —
applied one level up: a fix to *model-facing* text needs the same "did it actually change the
model's behavior" verification as a fix to a scanner's config, and reading the prompt and reasoning
about it is not a substitute for calling the API and checking.

### Two failure modes that looked identical from the CLI output turned out to have nothing in common

The user's original report bundled three questions together, all returning some form of "can't
help with that." They looked like the same bug. They weren't: the RDS questions failed because the
running container's image predated dynamic dispatch by twelve hours (a deployment staleness issue,
fixed by rebuilding, zero code changes needed); the VPC question failed because of a genuine,
reproducible model routing bug (a code/prompt issue, needing an actual fix). Treating "three
questions gave a similar-looking wrong answer" as "one bug with three symptoms" would have led to
either over-fixing the routing logic for a problem that was actually just a stale image, or
under-fixing it by assuming a rebuild would resolve all three. Isolating each question and testing
it independently — the container's build timestamp against git history for one, a direct multi-run
API test for the other — was necessary to tell them apart before touching anything.

## Session 3 — a read-only dashboard on the audit-log service (2026-08-06)

### "Verify it renders" means actually rendering it, not trusting the API response

Confirming `/stats` returns the right numbers proved the backend logic was correct — it said nothing
about whether the HTML/CSS/JS consuming those numbers actually produces a legible page. Headless
Chrome (already installed, no new dependency) rendered the real page against the real running
container, in both light and dark mode, and the result was screenshotted and looked at directly —
catching a real (if minor) issue a JSON check never could: the categorical chart's row order was
whatever SQLite's `GROUP BY` happened to return (alphabetical), not the fixed registry → fallback →
suggestion order the color mapping was designed around. Nothing was *broken* — colors still mapped
correctly to identities — but the display order silently drifting from the codebase's own canonical
ordering (`AnswerPath`'s literal order) is exactly the kind of small inconsistency that only shows
up by looking, never by asserting on individual field values.

### Consolidating duplicated "trusted raw SQL" into one spot pays for itself immediately

`stats()`'s aggregation queries were originally written the same way `query()` already was — an
f-string built inline, passed straight to `conn.execute()`. Semgrep flagged it five times, one per
query. The reflex might be five `# nosemgrep` comments, each repeating the same justification. Doing
that once, in a single `_raw_query` helper both methods now share, made the fix arrive as a two-fer:
one comment instead of five (with the same "verify it worked" discipline — re-run Semgrep, get zero
findings, don't just trust that consolidation looks like it should have fixed it), and less
duplicated SQL-execution code overall. Worth noticing as a general instinct: a Semgrep finding that
repeats itself across near-identical call sites is often a hint that those call sites should share
one implementation, not that they each need their own suppression.

### A demo-impressing feature is a good forcing function for revisiting old shortcuts

Adding the dashboard wasn't the only motivation for the `_raw_query` consolidation above — it also
quietly cleaned up something that was already slightly awkward in `query()` (an inline suppression
comment sitting right in the middle of business logic). New, visible feature work is often the
moment small existing debts actually get worth paying off, because the code has to be touched and
re-verified anyway — the marginal cost of also fixing the adjacent thing is much lower than doing it
as its own standalone change later.

## Session 3 — a large live test pass (2026-08-06)

### Fixing "the case I found" isn't the same as fixing "the pattern that caused it"

`ec2_uptime` over-matching "what VPCs exist" got fixed by excluding VPCs and subnets by name.
That was treating the symptom, not the mechanism — the actual cause was that a tool *named* after
a service reads, to the model, as covering the whole service, and no amount of listing individual
exclusions closes that gap for the next resource type someone asks about. Sure enough, EBS
snapshots and Elastic IPs reopened the identical bug days later. The durable fix wasn't a longer
exclusion list — it was naming the *general rule* ("a tool named after a service does not mean it
covers every resource type in that service") directly in the routing instructions, with one worked
example, rather than accumulating a growing list of specific patches. Worth generalizing: when a
bug's fix is "add this one case to an exclusion list," ask whether the list is actually enumerable
(a fixed, small set) or a symptom of a broader pattern that will keep producing new cases.

### The same fix doesn't always work when applied to two different layers

Tightening `ec2_uptime`'s own tool description fixed EBS snapshots immediately but did nothing for
Elastic IP addresses — repeatedly, 3/3, even after also adding a same-meaning hint to the dispatch
tool's own allow-list entry. What actually worked was a worked counter-example placed in
`SYSTEM_PROMPT`'s own routing instructions instead — a different layer of the prompt entirely. The
likely reason: "Elastic IP" has no obvious lexical link to the boto3 operation name
(`describe_addresses`), unlike "VPCs"→`describe_vpcs` or "snapshots"→`describe_snapshots`, so no
amount of describing what a tool *excludes* helps the model figure out what the *right* tool
actually is — it needs to see the mapping stated directly, once, as an example. Lesson: when a
fix that worked for one case doesn't transfer to a superficially similar one, don't assume it needs
more of the same intervention harder — it might need a different kind of intervention entirely, and
the fastest way to know is to just try one candidate fix at a time and check.

### An AWS SSO session dying mid-test-run doesn't look like a permissions problem — it looks like everything failing

Partway through a ~140-question live pass, every subsequent AWS-dependent question started failing
with the generic "I couldn't complete that AWS call right now" message — indistinguishable, from
the outside, from a permissions or code regression, and alarming to see as a wave of failures on the
live dashboard. The actual cause: the local SSO session (the same one every AWS CLI command in this
project depends on) had simply expired an hour or so into testing, and boto3's `TokenRetrievalError`
isn't a `ClientError` with an access-denied code, so it correctly fell through to the generic
message rather than the "insufficient access" one — which, on reflection, is the *right* answer for
this specific failure (retrying really does help, once the session is refreshed, unlike a genuine
permissions gap). The lesson isn't a code fix — it's a diagnostic habit: when a burst of failures
appears suddenly partway through a long run, check the mundane, boring explanation (did a credential
or session just expire) before assuming the newer, more interesting explanation (a routing or
permissions regression) — cheap to check first, and it was actually right here.

### "Shorter" and "reliable" are not the same axis, and conflating them causes real regressions

Asked to make the system prompt smaller and less restrictive, the first draft cut it roughly in
half — including replacing an imperative instruction-plus-worked-example with a shorter, softer
general statement. It read as an obvious improvement: same meaning, fewer words. Tested against the
exact cases already fixed once, it wasn't the same meaning at all — "which RDS instance is newest?"
went from reliably shaped to correct only half the time, and S3 object listing stopped working
entirely. The words that got cut for brevity weren't decorative; they were the specific thing
earlier testing had already proven necessary. The general lesson: when a prompt fix is "add a
concrete worked example," that example is doing real work a shorter paraphrase of the same idea
does not reliably do — model behavior responds to *specific, concrete instances* in ways that don't
transfer smoothly to *more efficiently phrased generalizations* of the same instruction. Brevity and
reliability can both be worth optimizing for, but they're separate axes, and improving one can
silently cost the other — the only way to know is to test the shorter version against every case
the longer version was already fixed for, not just read it and judge whether it "seems" clear.

### Restoring what regressed can restore more than intended, and needs its own re-verification

Adding back the RDS worked example (to fix its own regression) also happened to fix "what Elastic IP
addresses" — a case it wasn't written for — while a completely different rephrasing had separately
left that same case at 0/6. Fixes to a shared resource (one prompt, serving many routing decisions)
don't stay scoped to the case that motivated them — a change made for reason A can move the needle
on unrelated case B, in either direction, as a side effect of how the model weighs the prompt as a
whole. This is exactly why the final version was checked across *all* previously-fragile cases
together, 6 runs each, rather than just re-confirming the one case a given change was meant to fix —
a locally-correct-looking fix can still leave (or create) a regression somewhere else in the same
shared artifact.
