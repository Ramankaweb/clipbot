# Node 20+ is required by yt-dlp's YouTube JS solver (Debian's apt nodejs is too old)
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PYTHONUNBUFFERED=1
# IMPORTANT: 1 worker only (background threads + in-memory state live in this process)
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 120"]
