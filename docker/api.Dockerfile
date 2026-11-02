# API image: FastAPI under uvicorn. No PCL here — the API never imports pclapp.
#
#   docker build -f docker/api.Dockerfile -t pclapp-service-api .
#
# The same image runs the migrations (see docker-compose.yml, service "migrate").

FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY app ./app
COPY worker ./worker
RUN python -m venv /opt/venv && /opt/venv/bin/pip install .

FROM python:3.12-slim

RUN useradd --system --uid 1001 --create-home app
COPY --from=build /opt/venv /opt/venv
# Alembic needs the migration scripts at runtime, not just the installed package.
COPY --chown=app:app alembic.ini /srv/alembic.ini
COPY --chown=app:app alembic /srv/alembic

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PCLAPP_DATA_DIR=/data
RUN mkdir -p /data && chown app:app /data
VOLUME /data
WORKDIR /srv
USER app
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
