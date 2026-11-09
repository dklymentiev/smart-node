FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

ARG APP_VERSION=dev
ARG ENV_LABEL=

RUN sed -i "s/__BUILD_TS__/$(date +%m%d-%H%M)/" ./frontend/index.html &&     sed -i "s/__APP_VERSION__/${APP_VERSION}/" ./frontend/index.html &&     sed -i "s/__ENV_LABEL__/${ENV_LABEL}/" ./frontend/index.html

RUN adduser --disabled-password --no-create-home app && mkdir -p /data /data/transcripts && chown -R app:app /data
USER app

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8001"]
