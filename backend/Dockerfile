FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
# The prompt eval set: Settings → AI model scores models with it.
COPY evals ./evals

# Candle cache, liquidation history and alerts live here; mount a volume to keep them across deploys.
RUN mkdir -p /app/.cache && useradd --create-home appuser && chown -R appuser /app
USER appuser
VOLUME ["/app/.cache"]

EXPOSE 8000
# One worker: alerts and the Binance streams run in-process (TRUST_PROXY handles client IPs behind a proxy).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
