FROM python:3.12-slim

LABEL org.opencontainers.image.title="email-concierge"
LABEL org.opencontainers.image.description="Self-hosted IMAP-to-CalDAV event extractor (deterministic + plugin + NER + LLM pipeline)"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/nebriv/emailConcierge"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata: lets the listener resolve EMAIL_CONCIERGE_USER_TIMEZONE
# ca-certificates: TLS to IMAP/CalDAV/LLM endpoints
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md VERSION ./
COPY email_concierge ./email_concierge

# INSTALL_EXTRAS: pip extras selector, e.g. `ml` pulls in torch/sentence-
# transformers/gliner (~2GB) for Stage 3. Default is base — Stage 3 then
# degrades cleanly and the pipeline runs 1, 2, 4 only.
ARG INSTALL_EXTRAS=""
RUN if [ -n "$INSTALL_EXTRAS" ]; then \
      pip install ".[${INSTALL_EXTRAS}]"; \
    else \
      pip install .; \
    fi

RUN useradd --system --uid 1000 concierge \
    && mkdir -p /data \
    && chown concierge:concierge /data
USER concierge

VOLUME ["/data"]

ENV EMAIL_CONCIERGE_DB_PATH=/data/email-concierge.db \
    EMAIL_CONCIERGE_MODELS_DIR=/data/models \
    EMAIL_CONCIERGE_LOG_JSON=true

# Default is the interactive shell: REPL + listener as a daemon thread.
# When stdin isn't a TTY (e.g. `docker compose up -d` without
# `stdin_open: true`), the shell falls back to foreground-listener,
# so the container is still useful in a purely detached deployment.
# Attach with `docker attach email-concierge` to get the prompt.
ENTRYPOINT ["python", "-m", "email_concierge"]
CMD ["shell"]
