# ─────────────────────────────────────────────────────────────────────────────
# Spice Town Labels — container image (Python 3.12)
# Solves "wrong Python version" forever: the image bundles 3.12 + all deps +
# the DejaVu fonts the label renderer needs.
#
# Build:  docker build -t spicetown-labels .
# Run:    docker run -p 8080:8080 spicetown-labels      (file/null printing)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# System deps:
#   - libfreetype/zlib/jpeg: Pillow runtime (wheels usually bundle these, but
#     keep them for safety on slim)
#   - fonts-dejavu-core: the label fonts (DejaVuSans / -Bold)
#   - cups-client: provides `lp`/`lpstat` so the 'cups' transport can talk to a
#     networked CUPS server (set STL_PRINT_TRANSPORT=cups + CUPS_SERVER=...)
#   - curl: container HEALTHCHECK
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        cups-client \
        curl \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STL_ENV=production \
    STL_DATA_DIR=/data \
    STL_LOG_DIR=/data/logs \
    STL_DB_PATH=/data/spicetown.db \
    STL_PRODUCTS_FILE=/data/products.csv \
    STL_PRINT_TRANSPORT=file \
    STL_PRINT_SPOOL_DIR=/data/spool

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt gunicorn

COPY . .

# Seed /data with sample products on first run if none is mounted/provided.
RUN mkdir -p /data/logs /data/spool && cp -n data/products.csv /data/products.csv || true

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

# Single worker (-w 1) keeps the print queue/scheduler singular; threads give
# request concurrency.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-k", "gthread", \
     "-b", "0.0.0.0:8080", "--timeout", "120", "wsgi:app"]
