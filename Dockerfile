FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/cache \
    CACHE_DIR=/data/cache

WORKDIR /app

# Dependencias primero (capa cacheada mientras no cambie requirements.txt)
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY pytest.ini .

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/cache \
    && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=4)" || exit 1

# UN solo worker: el estado de los trabajos (202 + job) vive en memoria del proceso.
# El trabajo pesado (FITS, reproyección, PNG) corre en hilos, así que un worker aprovecha varios núcleos.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
