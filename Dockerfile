FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY email_concierge ./email_concierge

RUN pip install .

RUN useradd --system --uid 1000 concierge \
    && mkdir -p /data \
    && chown concierge:concierge /data
USER concierge

VOLUME ["/data"]

ENV EMAIL_CONCIERGE_DB_PATH=/data/email-concierge.db \
    EMAIL_CONCIERGE_MODELS_DIR=/data/models

ENTRYPOINT ["python", "-m", "email_concierge"]
CMD ["run"]
