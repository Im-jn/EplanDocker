FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EPLAN_STORAGE_ROOT=/app/storage \
    EPLAN_API_URL=http://api:8000

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pdf_parser ./pdf_parser
COPY worker_service ./worker_service
COPY eplan_runtime.py ./

RUN useradd --create-home --uid 10001 eplan \
    && mkdir -p /app/storage \
    && chown -R eplan:eplan /app
USER eplan

CMD ["python", "-m", "worker_service.main"]
