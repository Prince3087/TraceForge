FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

# libcairo2: native dependency of cairosvg (AOI SVG -> PNG for Gemini multimodal input)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY data/ data/
COPY .streamlit/ .streamlit/
COPY deploy/entrypoint.sh deploy/entrypoint.sh

# Non-root runtime user. /app/data stays writable: it is the local buffer of the audit ledger
# (the durable copy lives in GCS when LEDGER_BUCKET_NAME is set).
RUN chmod +x deploy/entrypoint.sh \
    && useradd --create-home --uid 10001 traceforge \
    && chown -R traceforge:traceforge /app
USER traceforge

EXPOSE 8080
# The internal API port is fixed (127.0.0.1:8000) when TRACEFORGE_MODE=all (default).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

ENTRYPOINT ["deploy/entrypoint.sh"]
