# syntax=docker/dockerfile:1.7

FROM rust:bookworm AS recorder-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build/upstream-biliup
COPY upstream-biliup/ ./
RUN cargo build --release -p biliup-cli


FROM python:3.11-slim-bookworm AS python-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY y2a-auto/requirements.txt ./requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && (/opt/venv/bin/pip install \
          "torch==2.6.0" "torchaudio==2.6.0" \
          --index-url https://download.pytorch.org/whl/cpu \
        || /opt/venv/bin/pip install "torch==2.6.0" "torchaudio==2.6.0") \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.11-slim-bookworm AS runtime

ARG TARGETARCH
ARG DENO_VERSION=2.4.5

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PORT=5001 \
    AUTO_START_RECORDER=1 \
    BILIUP_BIN=/app/upstream-biliup/target/release/biliup \
    PATH=/app/y2a-auto/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/home/biliup-y2a \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg gosu libfontconfig1 libfreetype6 \
        libfribidi0 libgomp1 libharfbuzz0b libsndfile1 libsox3 \
        libssl3 libunistring2 libxml2 passwd tzdata unzip \
    && arch="${TARGETARCH:-amd64}" \
    && case "$arch" in \
         amd64) deno_arch="x86_64-unknown-linux-gnu" ;; \
         arm64) deno_arch="aarch64-unknown-linux-gnu" ;; \
         *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL \
         "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_arch}.zip" \
         -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && chmod 0755 /usr/local/bin/deno \
    && rm -f /tmp/deno.zip \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --home-dir /home/biliup-y2a --shell /bin/bash biliup-y2a

WORKDIR /app
COPY --from=python-builder /opt/venv/ /app/y2a-auto/.venv/
COPY --from=recorder-builder \
    /build/upstream-biliup/target/release/biliup \
    /app/upstream-biliup/target/release/biliup
COPY . /app
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod 0755 \
      /usr/local/bin/docker-entrypoint.sh \
      /app/upstream-biliup/target/release/biliup \
    && mkdir -p /data \
    && chown -R biliup-y2a:biliup-y2a /app /home/biliup-y2a

EXPOSE 5001
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/', timeout=5)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "/app/run.py"]
