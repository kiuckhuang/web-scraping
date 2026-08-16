FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

RUN test "${APP_UID}" -ne 0 && test "${APP_GID}" -ne 0 && \
    groupadd -g ${APP_GID} appuser && \
    useradd -u ${APP_UID} -g ${APP_GID} -m appuser

WORKDIR /app

# Upgrade the base image's pip: python:3.12-slim ships an old pip missing the
# 2026 security fixes (PYSEC-2026-196/1795/1796/2875/2876). Build-time only.
RUN pip install --no-cache-dir -U pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir pytest==9.1.1

COPY server.py .
COPY tests/ ./tests/

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 9100

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9100"]
