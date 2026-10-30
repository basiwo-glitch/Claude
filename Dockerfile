FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    LOG_DIR=/data/logs TZ=Europe/Berlin
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 trader && mkdir -p /data/logs && chown -R trader /data

COPY requirements.txt requirements-dev.txt ./
# dev requirements (pytest, pandas) are included so the test suite can be run on the server
RUN pip install -r requirements-dev.txt

COPY app ./app
COPY tests ./tests
USER trader

EXPOSE 8000
# default: API; the worker service overrides the command (see docker-compose.yml)
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
