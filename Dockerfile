# mmex_web: https://hub.docker.com/_/python/tags
FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY engine ./engine
COPY web ./web

# Unprivileged by default; compose can override with `user: "<uid>:<gid>"`
# to match the owner of the MMEX folder.
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -M -d /app -s /usr/sbin/nologin app
USER app

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# One worker on purpose: a single process is the single writer.
# --no-control-socket: gunicorn 26 otherwise tries to create ~/.gunicorn, which
# the non-root user cannot, and the app has no use for the control interface.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--no-control-socket", "web:create_app()"]
