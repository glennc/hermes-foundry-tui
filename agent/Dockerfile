FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERMES_GATEWAY_SRC_ROOT=/app/third_party/hermes \
    HERMES_GATEWAY_PYTHON=/usr/local/bin/python

WORKDIR /app

COPY agent/requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

COPY third_party/hermes /app/third_party/hermes
RUN pip install --no-cache-dir /app/third_party/hermes

COPY agent /app/agent

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
WORKDIR /app/agent

EXPOSE 8088

CMD ["python", "main.py"]
