FROM python:3.12-slim

# ImageMagick and zip tools are used by the EPUB processor.
RUN apt-get update \
 && apt-get install -y --no-install-recommends imagemagick zip unzip coreutils bash \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py epub-shrink.sh ./
RUN chmod +x epub-shrink.sh \
 && useradd --system --uid 10001 --no-create-home app

USER app

ENV PORT=8080 \
    MAX_UPLOAD_MB=500 \
    LOG_LEVEL=INFO

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'))" || exit 1

CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT} -w 2 --threads 2 --timeout 900 app:app"]
