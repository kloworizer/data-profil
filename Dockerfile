FROM python:3.12-slim-bookworm

# --build-arg WITH_SPARK=1 : Java 17 + PySpark. The same image then also runs the
#                            Spark standalone master / worker (see docker-compose.yml),
#                            so Python, Spark and libraries match on every node.
#                            PYSPARK_VERSION must match the cluster when using an external one.
# --build-arg WITH_OCR=1   : Tesseract OCR for scanned PDFs.
# --build-arg WITH_DOCLING=1 : Docling TableFormer (CPU PyTorch) as a second PDF table reader,
#                            models baked into the image (nothing downloads at runtime).
ARG WITH_SPARK=0
ARG WITH_OCR=0
ARG WITH_DOCLING=0
ARG PYSPARK_VERSION=3.5.3

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AUTOEDA_DATA_DIR=/data AUTOEDA_INPUT_DIR=/input \
    OLLAMA_HOST=http://ollama:11434 \
    SPARK_HOME=/usr/local/lib/python3.12/site-packages/pyspark \
    PYSPARK_PYTHON=python3 \
    PATH="/usr/local/lib/python3.12/site-packages/pyspark/bin:${PATH}" \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# Extra root CAs for HTTPS-inspecting proxies / antivirus (certs/*.crt, optional)
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates >/dev/null 2>&1 || true

RUN pkgs=""; \
    if [ "$WITH_SPARK" = "1" ]; then pkgs="$pkgs openjdk-17-jre-headless procps"; fi; \
    if [ "$WITH_OCR" = "1" ]; then pkgs="$pkgs tesseract-ocr tesseract-ocr-ind"; fi; \
    if [ -n "$pkgs" ]; then apt-get update && apt-get install -y --no-install-recommends $pkgs && \
      rm -rf /var/lib/apt/lists/*; fi

WORKDIR /app
COPY requirements.txt requirements-docling.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$WITH_SPARK" = "1" ]; then pip install --no-cache-dir "pyspark==${PYSPARK_VERSION}"; fi && \
    if [ "$WITH_OCR" = "1" ]; then pip install --no-cache-dir pytesseract; fi && \
    if [ "$WITH_DOCLING" = "1" ]; then \
      pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
      pip install --no-cache-dir -r requirements-docling.txt && \
      docling-tools models download layout tableformer -o /app/models/docling; fi
ENV AUTOEDA_DOCLING_MODELS=/app/models/docling HF_HUB_OFFLINE=1

COPY autoeda ./autoeda
COPY scripts ./scripts
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh && \
    useradd -m -u 1000 app && mkdir -p /data /input && chown -R app /data /input

# The entrypoint starts as root only to fix /data ownership, then runs everything as `app`.
VOLUME ["/data", "/input"]
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["web"]
