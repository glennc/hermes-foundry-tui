FROM python:3.12-slim

WORKDIR /app

COPY . .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8088

CMD ["python", "main.py"]
