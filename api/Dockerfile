FROM python:3.12-slim

# ffmpeg solo para las miniaturas del inventario; el video en vivo lo maneja
# MediaMTX, que no pasa por aquí.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

ENV CAMWAY_DB=/data/camway.db \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8000

# Sin esto, "restart: unless-stopped" solo detecta que el proceso murió (exit
# code), no que quedó colgado vivo (deadlock de asyncio, etc.) — en ese caso
# el contenedor sigue "corriendo" para Docker y nunca se reinicia solo.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
