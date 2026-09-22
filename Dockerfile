FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEB_CONCURRENCY=1

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 1000 app
COPY --chown=app:app . .
USER app

# Listens on $PORT when the host sets one (Render does), else 7860.
ENV PORT=7860
EXPOSE 7860

# python:slim has no curl, so probe with the stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\", timeout=4).status == 200 else 1)"

# Worker count comes from WEB_CONCURRENCY (uvicorn reads it when --workers is omitted).
# Each worker keeps its own pool and polls Reddit itself, so 1 is the sensible default.
# `exec` so uvicorn is PID 1 and receives SIGTERM directly.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port $PORT --loop uvloop --http httptools --proxy-headers --forwarded-allow-ips '*'"]
