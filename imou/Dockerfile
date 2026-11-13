FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir httpx==0.28.1
WORKDIR /app
COPY connector.py /app/connector.py
CMD ["python", "connector.py"]
