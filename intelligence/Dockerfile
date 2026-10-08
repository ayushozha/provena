FROM python:3.14.8-slim-trixie

WORKDIR /app

COPY pyproject.toml requirements.lock /app/
COPY app /app/app
COPY eval /app/eval

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/app

EXPOSE 8081

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
