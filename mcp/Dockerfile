FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

RUN groupadd -g ${APP_GID} appuser && \
    useradd -u ${APP_UID} -g ${APP_GID} -m appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir pytest

COPY server.py .
COPY tests/ ./tests/

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 9100

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9100"]
