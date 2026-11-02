# Worker image: the service plus pclapp, which means PCL.
#
#   docker build -f docker/worker.Dockerfile -t pclapp-service-worker .
#
# Ubuntu 24.04 rather than python:3.12-slim because PCL comes from apt (libpcl-dev 1.14)
# and Ubuntu 24.04 ships Python 3.12, which is what pclapp's wheels are built for.
# Stage 1 compiles the pybind11 extension against libpcl-dev; stage 2 keeps only the
# runtime shared libraries, so the final image is a fraction of the build stage.

ARG PCLAPP_REF=main

# ---- stage 1: build ---------------------------------------------------------------------
FROM ubuntu:24.04 AS build
ARG PCLAPP_REF

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
        libpcl-dev python3-dev python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
# pclapp first: it is the slow, rarely changing layer (compiles against PCL).
RUN /opt/venv/bin/pip install "git+https://github.com/HatemSelim94/PCL-App@${PCLAPP_REF}"

WORKDIR /src
COPY pyproject.toml README.md ./
COPY app ./app
COPY worker ./worker
RUN /opt/venv/bin/pip install .

# ---- stage 2: runtime -------------------------------------------------------------------------
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 libpcl-io1.14 libpcl-filters1.14 libpcl-search1.14 libpcl-segmentation1.14 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 1001 --create-home app

COPY --from=build /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PCLAPP_DATA_DIR=/data \
    PCLAPP_PROCESSOR=pclapp
RUN mkdir -p /data && chown app:app /data
VOLUME /data
USER app

# SIGTERM makes the worker finish its current job and exit; compose sends it on "down".
STOPSIGNAL SIGTERM
CMD ["pclapp-worker"]
