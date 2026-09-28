#!/usr/bin/env bash
# Always rebuilds both images before running — a stale image silently
# answering with old code already happened once (docs/context.md section 8,
# 2026-08-06: the agent image was 12 hours older than the code that added
# dynamic dispatch, and every question fell back to the suggestion tier with
# no error at all). Don't skip the build step to "save time"; that's exactly
# what causes this class of bug.
#
# Usage, from CloudCopilot/:
#   scripts/ask.sh "which ec2 instances have been running more than 24 hours?"
#   scripts/ask.sh                      # interactive session, no question
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose build agent audit-log
docker compose up -d audit-log
docker compose run --rm agent "$@"
