FROM python:3.10-slim

ARG GOOSE_VERSION=v1.19.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    bash \
    bzip2 \
    ca-certificates \
    curl \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    git \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /usr/local/bin \
    && curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh \
    | GOOSE_VERSION="${GOOSE_VERSION}" CONFIGURE=false GOOSE_BIN_DIR=/usr/local/bin bash \
    && goose --version

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY axle_cli /app/axle_cli

RUN pip install -e .

EXPOSE 8080

CMD ["axle", "status"]
