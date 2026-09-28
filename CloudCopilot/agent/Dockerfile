FROM python:3.12.13-slim

WORKDIR /app

# Only the runtime dependencies — requirements-dev.txt (pytest, moto, ruff) is
# for testing/linting, never needed by (or copied into) the running image.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/

# agent/permissions.py reads this at import time to gate capabilities against
# the actually-deployed role (docs/context.md section 8) — nothing else under
# iam/ or ci/ is read by the running agent, so only this one file is copied in.
COPY iam/granted-actions.json ./iam/granted-actions.json

# Non-root, and the app directory is owned by that user before switching to
# it — FileAuditLog's default local sink creates a new directory/file at
# runtime, which needs write access (see audit_log/Dockerfile's same fix).
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["python", "-m", "agent"]
