FROM python:3.12-slim

WORKDIR /app

COPY app /app/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "fastapi>=0.116.0" \
    "uvicorn>=0.35.0" \
    "pydantic>=2.11.0" \
    "pydantic-settings>=2.7.0" \
    "httpx>=0.27.0" \
    "numpy>=1.26.0"

ENV PYTHONPATH=/app

EXPOSE 8081

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
