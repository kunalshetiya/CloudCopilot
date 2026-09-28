FROM python:3.12.13-slim

WORKDIR /app

# Copied and installed before the rest of the app code so this layer only
# gets invalidated (and re-run) when dependencies actually change, not on
# every code edit.
COPY audit_log/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY audit_log/ ./audit_log/

# Never run as root: an unprivileged user limits what an exploited bug in
# this service could do inside the container. Files copied above are owned
# by root (COPY runs before USER switches); chown so appuser can actually
# write the SQLite file it creates at startup.
#
# /data is a separate directory from the app code specifically so a Compose
# volume can be mounted there for persistence without hiding app.py/storage.py
# underneath it (see storage.py's DEFAULT_DB_PATH comment).
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser

ENV AUDIT_LOG_DB_PATH=/data/audit.db

EXPOSE 8001

CMD ["uvicorn", "audit_log.app:app", "--host", "0.0.0.0", "--port", "8001"]
