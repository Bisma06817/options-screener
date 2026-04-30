FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends cron tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src ./src
COPY tests ./tests

# Default command: run a single scan. The cron job on the host (or the
# scheduler in the entrypoint script) calls this hourly; the script itself
# decides whether to run based on the Sheet's scan_time_et value.
ENTRYPOINT ["python", "-m", "screener.main"]
