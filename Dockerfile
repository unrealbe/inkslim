FROM python:3.12-slim

ARG INKSLIM_VERSION=development
ARG INKSLIM_COMMIT=unknown

# ImageMagick and zip tools are used by the EPUB processor. Tesseract and its
# language data are installed now so every published architecture has the
# same OCR runtime available to the dedicated worker service.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      imagemagick zip unzip coreutils bash \
      tesseract-ocr \
      tesseract-ocr-eng \
      tesseract-ocr-nld \
      tesseract-ocr-fra \
      tesseract-ocr-deu \
 && rm -rf /var/lib/apt/lists/* \
 && tesseract --version \
 && langs="$(tesseract --list-langs 2>&1)" \
 && printf '%s\n' "$langs" | grep -Fxq eng \
 && printf '%s\n' "$langs" | grep -Fxq nld \
 && printf '%s\n' "$langs" | grep -Fxq fra \
 && printf '%s\n' "$langs" | grep -Fxq deu

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py epub-shrink.sh ./
COPY static ./static
COPY conversion ./conversion
RUN chmod +x epub-shrink.sh \
 && useradd --system --uid 10001 --no-create-home app \
 && mkdir -p /data/conversion \
 && chown -R app:app /data/conversion

USER app

ENV PORT=8080 \
    MAX_UPLOAD_MB=500 \
    LOG_LEVEL=INFO \
    INKSLIM_DATA_DIR=/data/conversion \
    INKSLIM_VERSION=\${INKSLIM_VERSION} \
    INKSLIM_COMMIT=\${INKSLIM_COMMIT}

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'))" || exit 1

CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT} -w 2 --threads 2 --timeout 900 app:app"]
