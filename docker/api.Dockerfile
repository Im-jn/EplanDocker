FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EPLAN_STORAGE_ROOT=/app/storage

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY api_service ./api_service
COPY pdf_parser ./pdf_parser
COPY scripts ./scripts
COPY eplan_runtime.py ./

RUN useradd --create-home --uid 10001 eplan \
    && mkdir -p /app/storage \
    && chown -R eplan:eplan /app
USER eplan

EXPOSE 8000
CMD ["uvicorn", "api_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
