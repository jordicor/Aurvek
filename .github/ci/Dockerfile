FROM node:22-bookworm-slim AS node
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install --yes --no-install-recommends \
    python3.12 python3.12-venv build-essential libffi-dev \
    ffmpeg libmagic1 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /app
COPY requirements-linux.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --requirement requirements-linux.txt
COPY . .
CMD ["python", ".github/ci/check.py"]
