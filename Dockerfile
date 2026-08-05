# Python 3.11 — pyproject requires >=3.11.
FROM python:3.11-slim

WORKDIR /app

# Install deps first (better layer caching): copy metadata, then the source the
# editable install needs to resolve the engine/agents packages.
COPY pyproject.toml README.md ./
COPY engine ./engine
COPY agents ./agents
COPY scripts ./scripts
COPY config.py orchestrator.py transcript.py webapp.py ./

RUN pip install --no-cache-dir -e ".[web]"

# Root-level modules (config.py, orchestrator.py, ...) resolve via PYTHONPATH;
# the editable install covers the engine/agents packages.
ENV PYTHONPATH=/app \
    GAMES_DIR=/data/games \
    PORT=8080

EXPOSE 8080

# exec form via sh -c so ${PORT} expands AND uvicorn becomes PID 1, so Fly's
# SIGTERM reaches it for a clean shutdown.
CMD ["sh", "-c", "exec uvicorn webapp:app --host 0.0.0.0 --port ${PORT:-8080}"]
