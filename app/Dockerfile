FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY wapeople ./wapeople
COPY tests ./tests

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000

# Default command: the website. Hosts like Railway inject $PORT; docker-compose overrides
# this with its own commands for the web and sync services.
CMD ["sh", "-c", "python -m wapeople.migrate && exec uvicorn wapeople.web.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --workers ${WEB_WORKERS:-2}"]
